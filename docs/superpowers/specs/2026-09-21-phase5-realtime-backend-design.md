# Phase 5 — Backend Real-Time Layer: Design Spec

WebSockets, case queue, feedback loop, rule-threshold editing.
Branch: `phase5-realtime-backend` (off `phase4-realtime-pipeline`; PR #4 for
Phase 4 is open and does not gate this work).

## 1. Purpose and scope

Phase 4 flags suspicious transactions into `flagged_cases`, but nobody can
work those flags. Phase 5 makes that table a working case queue and closes
the loop between analyst decisions and detection.

**In scope**
1. Case queue: list pending cases, claim / release a case, submit a decision.
2. Live push of new and changed cases to clients over WebSockets, with Redis
   fan-out (mandated by the roadmap).
3. Feedback loop: analyst decisions are stored, and visibly affect future
   flags in this phase (priority demotion, section 8).
4. Rule-threshold editing API (`rules_config` write path deferred from
   Phase 3) with change history and hot reload into a running pipeline.
5. A nullable `ml_anomaly_score` column on the case schema from the start.
   Phase 7 populates it; Phase 5 only guarantees it exists and is returned
   by the API as `null`.

**Out of scope / deferred**
- UI (Phase 9), ML scoring (Phases 6-7), Claude summaries (Phase 6).
- Real authentication. `analyst` is a free-text field. **Phase 11 talking
  point:** identity is unauthenticated and trust-based, so claim ownership
  and decision attribution can be spoofed. Real identity is deferred.
- Phase 4 gaps stay deferred (rollup look-ahead and staleness, partition
  automation, Postgres reconnect in the pipeline, throughput-ceiling rerun).
  New Redis clients introduced here DO get reconnect handling (sections 5, 9).
- Rescoring existing cases after a rules change. Detection output is
  immutable (section 2).

## 2. Principles carried into this phase

- **Detection output is immutable once written.** `total_score`,
  `rule_results`, `flagged_at`, `transaction_*`, `user_id` are never updated
  after insert. Analyst work and prioritization live in separate columns/tables.
- **Every flag is explainable by a fired rule.** `is_flagged` (any rule fired)
  is unchanged. Nothing in this phase suppresses a flag.
- **Redis pushes are hints; REST is the source of truth.** Pub/sub is
  at-most-once, so every consumer has a REST/DB repair path.

### Decision: `ON CONFLICT (transaction_id) DO NOTHING` (open item from Phase 4)
Resolved explicitly rather than inherited:
- `DO NOTHING` stays as the mechanism, because first-write-wins is the
  correct behavior under immutability: a reprocessed transaction must not
  alter what an analyst already saw or acted on.
- It is no longer silent. `persist_flagged_case` returns whether it inserted
  (`INSERT ... RETURNING id`). On a skipped duplicate it logs both the stored
  and the newly computed `total_score` and `rules_config_version`.
- Events publish only when a row was actually inserted.
- Reprocessing with changed rules is not supported in Phase 5. If wanted
  later it goes in a separate history table, never an in-place update.
- A test covers it: insert, re-run with different config, stored row unchanged,
  no event published.

## 3. Migration 007 (one atomic migration, all Phase 5 schema)

One file, one transaction: `infra/migrations/007_case_queue_and_feedback.sql`.
Order matters for FKs: history table, then case_feedback, then the
`flagged_cases` alteration.

### 3.1 `rules_config_history` (new, append-only)
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | this id is the "rules config version" |
| rule_name | TEXT NOT NULL | |
| before | JSONB NULL | `{weight, enabled, params}`; NULL for seed rows |
| after | JSONB NOT NULL | full new state of that rule |
| changed_by | TEXT NOT NULL | free-text; seed rows use `'migration'` |
| changed_at | TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp() | |

The migration seeds one row per existing rule (the current `rules_config`
values). **Version definition:** the config version in effect at any moment
is `max(rules_config_history.id)`. The config at version V is reconstructed
as, per rule, the latest history row with `id <= V`.

### 3.2 `case_feedback` (new, append-only decision log)
| column | type | notes |
|---|---|---|
| id | BIGSERIAL PK | tiebreaker in the resolution rule |
| case_id | BIGINT NOT NULL REFERENCES flagged_cases(id) | |
| decision | TEXT NOT NULL CHECK IN ('confirmed_fraud','false_positive') | |
| analyst | TEXT NOT NULL | free-text, unauthenticated |
| note | TEXT NULL | |
| decided_at | TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp() | |

Index `(case_id, decided_at DESC, id DESC)`. Rows are never updated or
deleted. **Claiming does not write here** (section 4).

### 3.3 `flagged_cases` alteration (full new column set)
| column | type | notes |
|---|---|---|
| status | TEXT NOT NULL DEFAULT 'open' CHECK IN ('open','in_review','confirmed_fraud','false_positive') | projection, see section 4 |
| claimed_by | TEXT NULL | set by claim, cleared by release, retained after a decision for audit |
| claimed_at | TIMESTAMPTZ NULL | |
| ml_anomaly_score | DOUBLE PRECISION NULL | no default, no NOT NULL. Phase 7 populates it |
| rules_config_version | BIGINT NULL REFERENCES rules_config_history(id) | version in effect when scored. NULL only on pre-Phase-5 rows (not provable, left NULL, documented) |
| priority_score | DOUBLE PRECISION NOT NULL | queue sort key. Backfilled to `total_score` for existing rows |
| priority_adjustment | JSONB NULL | citation for demotion (section 8). NULL when not adjusted |

Constraints:
- `(claimed_by IS NULL) = (claimed_at IS NULL)`
- `status <> 'in_review' OR claimed_by IS NOT NULL`
- `status <> 'open' OR claimed_by IS NULL`

Indexes:
- `(status, priority_score DESC, id DESC)` for the queue.
- Partial `(user_id) WHERE status = 'false_positive'` for the demotion lookup.
- Partial `(user_id) WHERE status = 'confirmed_fraud'` for the confirmed-fraud veto.

**`ml_anomaly_score` is built here, in Task 1**, not left to Phase 7. A test
asserts the column exists, is nullable, has no default, and that the case
API returns it as `null`.

## 4. Case status model: two transition sources

Status vocabulary: `open`, `in_review`, `confirmed_fraud`, `false_positive`.
There are two distinct sources of transitions. They write different places
and must not be conflated.

### 4.1 Claim source: `open <-> in_review`
Written directly to `flagged_cases` (`claimed_by`, `claimed_at`, `status`),
never through `case_feedback`. Claiming is not a decision.

- **Claim**, one atomic conditional statement (not read-then-write):
  ```sql
  UPDATE flagged_cases
     SET status='in_review', claimed_by=%(analyst)s, claimed_at=clock_timestamp()
   WHERE id=%(id)s AND status='open'
  RETURNING ...;
  ```
  Rows affected = 1 means claimed. 0 means someone else got there first or
  the case is not open (`409`; a follow-up read distinguishes 404 from 409
  and names the current holder, for the error message only).
- **Release**:
  ```sql
  UPDATE flagged_cases
     SET status='open', claimed_by=NULL, claimed_at=NULL
   WHERE id=%(id)s AND status='in_review' AND claimed_by=%(analyst)s
  RETURNING ...;
  ```
  Release is restricted to `claimed_by == calling analyst`, symmetric with
  the claimant-only decision rule in 4.2: one analyst cannot release another's
  claim, and the `WHERE claimed_by=%(analyst)s` clause enforces that
  atomically. Rows affected is checked the same way as for claim (0 rows is
  `409`). Because `analyst` is unauthenticated free text (Phase 11 talking
  point), this prevents accidents, not impersonation.
- A concurrency test (real threads, real Postgres, N analysts claiming
  simultaneously) asserts exactly one wins.

### 4.2 Decision source: `open/in_review -> confirmed_fraud/false_positive`
Written to `case_feedback`, with `status` recomputed from the log.

**Resolution rule (stated, not implicit):** a case's current decision is the
`case_feedback` row with the greatest `(decided_at, id)`. `id` breaks
timestamp ties. Ordering is never left to query order.

**Projection rule** (the only place status is computed):
```
status = current decision, if any case_feedback row exists
       = 'in_review',       else if claimed_by IS NOT NULL
       = 'open',            otherwise
```

`submit_feedback` runs in one transaction:
1. `SELECT ... FROM flagged_cases WHERE id=%s FOR UPDATE`: serializes
   concurrent feedback and claims on the same case.
2. Insert the `case_feedback` row (`decided_at` uses `clock_timestamp()`,
   not `now()`, because `now()` is transaction start time and can invert
   relative to lock-acquisition order).
3. Recompute `status` from the log using the resolution and projection
   rules above. It **recomputes** and never writes the incoming value, so
   concurrent analysts cannot leave the column disagreeing with the log.
4. Commit, then publish `case.updated` (section 5).

**Policy** (proposed, for review):
- A decision on an `in_review` case is accepted only from its claimant
  (`409` otherwise).
- A decision on an `open` case (never claimed) is allowed.
- A correction on an already-decided case may be submitted by **any
  analyst**, and is appended. A later decision supersedes by the resolution
  rule. Reason for not restricting it: `case_feedback` is append-only and the
  `(decided_at, id)` rule already resolves conflicting decisions
  deterministically, with the full history preserved and attributed. Nothing
  is lost or overwritten, so a restriction would add friction without adding
  integrity. The claimant-only rule exists to stop two analysts working the
  same case simultaneously, and a decided case is no longer being worked, so
  that reason does not apply. Cost accepted: with unauthenticated `analyst`,
  anyone can flip a verdict, which is visible in the history and a Phase 11
  item.
- A decided case cannot be claimed (claim requires `status='open'`).
  Reopening is out of scope.

**Invariant:** the log is authoritative. `flagged_cases.status` is a
denormalized projection kept for indexed queue filtering. A test builds
random sequences of claims, releases and decisions (including out-of-order
`decided_at` and tied timestamps) and asserts the column equals the
projection recomputed from scratch.

## 5. Real-time delivery: WebSockets over Redis (Option A)

The pipeline is a separate CLI process from the API server. Redis carries
events between them.

### 5.1 Publish side: direct publish after commit
- New module `app/realtime/publisher.py` exposes
  `publish_case_event(event_type, case_summary)`.
- The pipeline callback calls it **after `conn.commit()` succeeds** and only
  when `persist_flagged_case` reported an insert. The case service also
  calls it after commit for claim, release and feedback.
- Channel `cases:events`. Message JSON:
  `{"type": "case.created" | "case.updated", "case": {id, transaction_id,
  user_id, total_score, priority_score, status, claimed_by,
  fired_rules: [...], ml_anomaly_score, flagged_at}}`.
- A publish failure is logged and counted, never raised: detection must not
  fail or roll back because Redis is down. The case is safe in Postgres.
- `publish_case_event` is the single indirection. Moving to a
  `LISTEN/NOTIFY` bridge or outbox later touches only this function's
  callers' wiring, not the pipeline or case service.

### 5.2 Subscribe and fan-out side
- One Redis subscriber per API instance (`redis.asyncio`), started in the
  FastAPI lifespan, with reconnect and backoff.
- Feeds an in-process `ConnectionManager` that fans out to that instance's
  WebSocket clients at `/ws/cases`.
- Each client has a bounded send queue. On overflow the client is
  disconnected, and it repairs via REST on reconnect. One slow client never
  blocks fan-out. Server sends periodic pings.

### 5.3 `since_id` REST-repair cursor
- `GET /cases?since_id=N` returns cases with `id > N`, `id ASC`, capped
  (default 200, max 500), any status. It is the repair path for missed
  pushes.
- Client protocol: connect the WebSocket first, then call REST with the last
  seen id, then dedupe by case id (events carry full summaries, so
  reapplying is idempotent).
- **Assumption, stated:** with a single writer, commit order equals `id`
  order, so `id > N` never skips a case. If multiple concurrent writers are
  ever introduced (LoadGenerator alongside the pipeline against the same
  table would qualify), sequence gaps can commit out of order and this
  cursor must be revisited (overlap window, or move to an outbox).
  Documented on the endpoint.

### 5.4 Infra
Redis 7 added to `infra/docker-compose.yml` with a healthcheck.
`REDIS_URL` env var (default `redis://localhost:6379/0`). `redis` (redis-py)
added to `backend/requirements.txt`, pinned. Tests run against a real Redis
per the definition of done.

## 6. REST API

Synchronous FastAPI endpoints with a `psycopg2` `ThreadedConnectionPool`.

| endpoint | purpose |
|---|---|
| `GET /cases?status=&limit=&cursor=` | queue: `ORDER BY priority_score DESC, id DESC`, keyset paginated |
| `GET /cases?since_id=N` | repair cursor (5.3) |
| `GET /cases/{id}` | detail: rule breakdown, `priority_adjustment`, `ml_anomaly_score`, feedback history |
| `POST /cases/{id}/claim` `{analyst}` | 4.1 |
| `POST /cases/{id}/release` `{analyst}` | 4.1 |
| `POST /cases/{id}/feedback` `{analyst, decision, note?}` | 4.2 |
| `GET /stats/rules` | per rule: counts of `confirmed_fraud` / `false_positive` among cases whose current status is decided and where that rule fired. Precision = confirmed / (confirmed + false_positive) |
| `GET /rules`, `GET /rules/history` | current config, change history (section 7) |
| `PATCH /rules/{rule_name}` | section 7 |
| `WS /ws/cases` | section 5 |

`/stats/rules` is the input analysts use to decide threshold edits.

## 7. Rule-threshold editing

### 7.1 `PATCH /rules/{rule_name}`
Body: any of `weight`, `enabled`, `params` (partial). At least one field.
`params` is a shallow merge over the existing params. Rule names cannot be
created or deleted (404 for unknown). `changed_by` is required (free-text).

**Validation** (`422` on failure; nothing written):
- `weight`: finite float, `>= 0`.
- `enabled`: bool. Rejected if it would leave **no** rule enabled (that would
  silently disable all detection).
- `params`: unknown keys rejected. Per rule:
  - `velocity`: `window_minutes` int `> 0`; `threshold_count` int `>= 2` and
    `<= 100`. The upper bound is `DEFAULT_ROW_CAP`: the loader only supplies
    that many recent transactions, so a larger threshold could never fire.
  - `amount_baseline`: `deviation_multiplier` finite float `> 1.0`.
  - `geo_impossibility`: `min_distance_km` finite float `>= 0`;
    `max_speed_kmh` finite float `> 0`.

**What weight does and does not do (stated explicitly).** `total_score` is
the weight-normalized average of enabled rules' sub-scores. Therefore
**`weight` affects only `total_score` (and so queue sort order via
`priority_score`), never whether a transaction is flagged.** Flagging is
"any enabled rule fired" and depends only on `enabled` and `params`. The
PATCH response includes `affects_flagging: bool` (true iff `enabled` or
`params` changed) and this note is in the endpoint docs, so nobody tunes
weights expecting flag changes.

**Write path.** One transaction: `SELECT ... FOR UPDATE` the `rules_config`
row, apply, update `updated_at`, insert the `rules_config_history` row
(`before`, `after`, `changed_by`), commit, then publish `rules:updated`
`{"version": <new history id>}`. Concurrent edits are last-write-wins, with
the history preserving every step.

### 7.2 Version stamping and hot reload
- New module `RulesProvider` holds an immutable `(rules_config, version)`
  snapshot. Both are read in one `REPEATABLE READ` transaction so they are
  consistent. The pipeline callback calls `provider.current()` per
  transaction and stamps `rules_config_version` on the case.
- The pipeline process subscribes to Redis channel `rules:updated`, reloads,
  and swaps the snapshot by a single reference assignment (no partial state
  visible mid-transaction). It loads on startup regardless.
- **Repair path:** every 30 s the provider compares `max(history.id)` to its
  version and reloads if behind, covering a lost message or Redis restart.
  The subscriber reconnects with backoff.
- Behavior around an edit: a transaction being scored during a swap uses one
  snapshot or the other, never a mix, and its case records the version it
  used.

## 8. Feedback affecting future flags: priority demotion (Option B)

Decisions change how future flags are **prioritized**, never whether they
are created. **This is a deliberate security property:** a false-positive
mark can only lower priority within a bounded range, and can never silence
a compromised account or rule.

**Inputs at flag time** (lookup runs only for transactions already flagged),
for transaction user U and each fired rule r:
- `n_r` = number of cases for U whose **current** status is `false_positive`
  and where rule r fired in `rule_results`. Because it uses current status,
  a later `confirmed_fraud` correction removes that case's influence.
- `veto` = U has any case whose current status is `confirmed_fraud`.

**Formula (`formula_version` 1).** Constants `DECAY = 0.8`, `FLOOR = 0.5`.
```
factor_r = 1.0                        if veto or n_r == 0
factor_r = max(FLOOR, DECAY ** n_r)   otherwise
adjusted_sub_score_r = sub_score_r * factor_r     (fired rules only)
priority_score = weighted average of adjusted sub-scores over enabled rules,
                 using the same combiner and weights as total_score
```
Unfired rules keep their sub-score. With no `n_r`, `priority_score ==
total_score` exactly. Guarantees, each covered by a test:
- `priority_score >= FLOOR * total_score` (bounded demotion).
- The case is still inserted and published (never suppressed).
- Any `confirmed_fraud` for the user removes all demotion for that user.
- `total_score` is untouched.

**Citation.** `priority_adjustment` (NULL when nothing was adjusted):
```json
{"formula_version": 1, "decay": 0.8, "floor": 0.5, "veto": false,
 "per_rule": [{"rule_name": "velocity", "prior_false_positive_count": 2,
               "prior_false_positive_case_ids": [41, 87], "factor": 0.64}]}
```
Cited case ids are the most recent 10, with the full count alongside. The
case detail endpoint returns it, so an analyst sees exactly why a case sits
lower. The constants are stored in the citation, so old cases stay
interpretable if constants change later.

**Deferred:** time-decay or expiry of old false-positive marks (unbounded
history currently counts). Noted as a limitation.

**Exit test (end to end, real DB):**
1. Baseline: transaction from user U fires rule R, so a case is created with
   `priority_score == total_score`.
2. Mark that case `false_positive`.
3. A new transaction from U fires R: a case is created (not suppressed) with
   `priority_score < total_score`, and the citation names the first case.
4. The same scenario without step 2 yields no adjustment.
5. Add a `confirmed_fraud` for U: the next flag shows no demotion.

## 9. Error handling

- Redis down at publish: log and count, detection unaffected (5.1).
- Redis down for the API subscriber: reconnect with backoff, and clients
  repair via `since_id` on reconnect.
- Pipeline Redis subscriber for rules: reconnect with backoff, with the
  30 s version comparison as the safety net.
- Postgres connection drops in the pipeline stay a Phase 4 deferred gap.
- API endpoints return `404` unknown, `409` claim/decision conflicts, `422`
  validation.

## 10. Testing approach

Real Postgres and real Redis, not mocked. Concurrency tests use real
threads. Property-style test for the status projection (4.2). Existing
57 tests must stay green throughout.

**Definition of done:** full test suite and build from the main tree, plus a
non-mocked smoke test: boot Postgres, Redis, the API and the pipeline; a
WebSocket client receives `case.created` for a real flagged transaction; a
PATCH to a threshold changes flagging in the running pipeline; a decision
demotes a later flag.

## 11. Task breakdown

1. **Migration 007 + schema tests.** `rules_config_history` (+ seed),
   `case_feedback`, `flagged_cases` alterations, constraints, indexes.
   Includes `ml_anomaly_score` nullability tests and the constraint tests.
2. **Redis infra + realtime primitives.** Compose service, dependency,
   config, `publish_case_event`, subscriber with reconnect. Real-Redis tests.
3. **Case repository.** Atomic claim / release, `submit_feedback`
   transaction, resolution and projection rules, concurrency test, projection
   property test.
4. **Pipeline integration.** `persist_flagged_case` returns inserted (the
   `ON CONFLICT` decision and test), version stamping via `RulesProvider`
   (read side), publish after commit.
5. **Case REST API.** List (queue + `since_id`), detail, claim, release,
   feedback, `/stats/rules`.
6. **WebSocket fan-out.** Lifespan subscriber, `ConnectionManager`, bounded
   queues, ping, repair protocol test.
7. **Rules admin + hot reload.** `GET/PATCH /rules`, validation, history,
   `affects_flagging`, `rules:updated`, running-pipeline reload test, 30 s
   version poll.
8. **Priority demotion (Option B).** Lookup, formula, citation, guarantees,
   the end-to-end exit test.
9. **Integration, smoke test, docs.** Full smoke test (section 10), progress
   log for Phase 5, README/CLAUDE.md updates.

Each task is reviewed before the next begins, matching Phases 3-4.
