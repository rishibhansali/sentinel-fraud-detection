# Phase 5 — Backend Real-Time Layer: Progress Log

Spec: `docs/superpowers/specs/2026-09-21-phase5-realtime-backend-design.md`
Branch: `phase5-realtime-backend` (off `phase4-realtime-pipeline`; PR #4 for
Phase 4 is open and unmerged). At the September 21 handoff, Phase 5 had not
been PR'd. Draft PR #5 was opened against the Phase 4 branch on October 7;
neither phase has been merged into `main`.

## Built (against the spec's 9-task breakdown)
Each task ran in its own worktree and landed as its own `git merge --no-ff`
commit, so any one can be undone with `git revert -m 1 <merge>`.

- **Task 1: Migration 007** (`infra/migrations/007_case_queue_and_feedback.sql`).
  One atomic file: `rules_config_history` (seeded, id = config version),
  `case_feedback` (append-only), and `flagged_cases` gains `status`,
  `claimed_by`, `claimed_at`, `ml_anomaly_score` (nullable, no default,
  tested), `rules_config_version` (FK), `priority_score` (backfilled, NOT
  NULL), `priority_adjustment`, plus the three CHECK constraints and three
  indexes. Matches spec section 3 column for column. 19 schema tests.
- **Task 2: Redis + realtime primitives.** `redis:7` compose service,
  `redis==6.4.0`, `app/realtime/` (`config`, `events`, `publisher`,
  `subscriber`). Publisher never raises (logged and counted). Two reconnecting
  subscribers (async and threaded) with exponential backoff and `on_reconnect`.
  18 tests against real Redis, including `CLIENT KILL TYPE pubsub` reconnect.
- **Task 3: Case repository** (`app/cases/repository.py`). Atomic
  claim/release (single conditional `UPDATE ... RETURNING`), `submit_feedback`
  (row lock, policy, insert, recompute), explicit `ORDER BY decided_at DESC,
  id DESC` resolution rule, projection rule. 8-thread claim race test and a
  260-step seeded projection property test. 14 tests.
- **Task 4: Pipeline integration.** `RulesProvider` (immutable snapshot,
  read in one REPEATABLE READ transaction), version stamping,
  `persist_flagged_case` returns id or `None` on conflict and logs the
  duplicate, publish `case.created` after commit only for inserted rows.
  10 tests.
- **Task 5: Case REST API.** Queue with keyset pagination (correct under
  ties), `since_id` repair cursor, detail, claim/release/feedback (404/409/422),
  `/stats/rules`, `case.updated` published after commit. 22 tests.
- **Task 6: WebSocket fan-out.** `ConnectionManager` (bounded per-client
  queues, non-blocking broadcast, slow-consumer eviction with 1013),
  `WS /ws/cases`, `create_app()` factory, one Redis subscriber per API
  instance. Tested over real sockets against real uvicorn servers, including
  two-instance fan-out through Redis. 11 tests.
- **Task 7: Rules admin + hot reload.** `GET /rules`, `GET /rules/history`,
  `PATCH /rules/{name}` with the spec's validation, atomic config+history
  write, `rules:updated` after commit; `RulesProvider.start_hot_reload`
  (Redis subscription, forced reload on reconnect, 30 s version poll). 68
  tests including a real running-pipeline PATCH proof.
- **Task 8: Priority demotion (Option B).** `app/pipeline/priority.py`:
  `DECAY=0.8`, `FLOOR=0.5`, `confirmed_fraud` veto, JSON citation on the case.
  28 unit/integration tests and the end-to-end exit test.
- **Task 9: Integration + smoke.** `scripts/smoke_phase5.py` (real
  Postgres, Redis, API process and pipeline process, 17 steps),
  `tests/integration/test_case_detail_priority.py`, `CLAUDE.md` updates.

## Verification (run from the MAIN tree after all merges)
- Full suite: `cd backend && .venv/bin/python -m pytest` -> **249 passed**
  (57 at the start of Phase 5). The 42 warnings are `DeprecationWarning`s from
  third-party `websockets`/`starlette`, none from project code.
- Smoke: `backend/.venv/bin/python scripts/smoke_phase5.py` -> **17/17 PASS**,
  and the DB (row counts, `rules_config` incl. `updated_at`, max history id) is
  identical before and after.
- Demotion exit test: `test_demotion_exit_criteria` passes (full output in the
  review message and reproducible with `pytest tests/pipeline/test_priority_exit.py -s -v`).
- There is no build step in this repo (Python backend only, no frontend build),
  so "build" is the import of the app plus the smoke boot.

## Spec vs. what was built: deviations, additions, and spec issues
Explicitly listed, none silent.

**Spec issues found while building against real code**
1. **Sequencing gap (fixed).** 007 makes `priority_score` NOT NULL, and the
   spec's Task 4 was where the pipeline INSERT changed. But the DB is shared,
   so Task 1 alone broke 5 Phase 4 tests. Fixed by landing a one-line
   `priority_score = total_score` in the INSERT immediately after Task 1
   (commit `ff5f450`); demotion logic still landed in Task 8.
2. **`veto` citation gap (resolved in the 2026-10-07 review).** The original
   spec made `priority_adjustment` NULL whenever the score was unchanged, so
   `veto` could never be true in a stored citation. When confirmed fraud now
   suppresses an otherwise applicable demotion, the case stores `veto: true`,
   `factor: 1.0`, and the false-positive counts and case ids. The priority
   remains equal to `total_score`. With no relevant false positives, the
   citation remains NULL.
3. **7.1 write path locks ONE row; the last-enabled guard needs ALL.** Two
   concurrent disables of different rules could each pass. Implemented as
   `SELECT ... FOR UPDATE` on all `rules_config` rows (all PATCHes serialize).
   Concurrency test proves exactly one of two racing disables succeeds.
4. **7.1 "finite float" vs int params.** Seeded `window_minutes` and
   `threshold_count` are ints, so validation is per-key int/float and rejects
   bool and non-integral floats for int keys.
5. **Sub-scores are unbounded** (geo reached ~83 in the exit test), so
   `total_score` above 1 is normal. The bound `priority >= 0.5 * total` still
   holds; note that demotion acts on the fired rule's sub-score inside a
   weighted average, so absolute demotion is smaller than the factor when
   other rules contribute.
6. **Test note for later authors:** scaling all rule weights by the same
   factor does not change `total_score` (weights are normalized); a
   "different config" test must change one weight only.

**Additions beyond the spec (inferences, each stated in code)**
- `{"type": "resync"}` broadcast to WebSocket clients when the API's Redis
  subscriber reconnects, so they repair via `since_id`.
- Default queue filter (no `status`) = `open` + `in_review` ("pending").
- `next_since_id` = last id of a full page, else null (caught up).
- No-op PATCH writes nothing (no history row, no version bump, no publish) and
  returns `changed: false`.
- PATCH also rejects unknown top-level keys, `params: {}` and non-object
  bodies.
- `/stats/rules` returns every rule in `rules_config` (zero counts) plus any
  other rule name seen in decided cases.
- Application-level `{"type": "heartbeat"}` rather than protocol pings.
- Cited ids in `priority_adjustment` are the newest 10, shown ascending.
- Subscriber backoff resets on a confirmed subscription (my tweak after Task 2
  review), not only after a delivered message.
- `RulesProvider.reload()` is lock-serialized so the Redis and poll threads
  cannot swap an older snapshot after a newer one.

**The two approved additions (from your review)**
- Release is claimant-only, symmetric with the claimant-only decision rule
  (spec 4.1; enforced by `WHERE claimed_by = %s` and tested).
- Correction on an already-decided case: **any analyst** may submit one
  (spec 4.2, with the reason stated: append-only log plus deterministic
  resolution rule preserve integrity; the claimant rule exists to prevent
  simultaneous work, which does not apply to decided cases). Tested.

**Decisions taken from Phase 4's open list**
- `ON CONFLICT (transaction_id) DO NOTHING` is now an explicit, tested
  decision: first write wins (immutability), never silent (WARNING with both
  scores and versions), no event on a skipped duplicate.

## Bugs caught and fixed along the way
- **Task 1:** the 5 red Phase 4 tests above (NOT NULL `priority_score`).
- **Task 2:** a stray `else: continue` after `finally` (syntax error) and a
  mid-function `global` declaration, both caught before any test run.
- **Task 4:** the agent's first duplicate-config test doubled every weight and
  the score did not move (weights are normalized). Fixed the test to change one
  weight; this also produced spec note 6.
- **Task 6:** the first slow-consumer test never triggered eviction because the
  `websockets` client library absorbs data into its own buffers. Switched to a
  raw non-reading socket. (Also surfaced the byte-bound limitation below.)
- **Task 7:** NaN/inf could not be sent through TestClient's `json=`; those
  cases now use raw JSON bodies. Per-test app startup made the suite ~4x
  slower, so the client fixture is module-scoped.
- **Task 8:** two of the agent's own test expectations were wrong (priority is
  not `total * factor` when other rules contribute); fixed the tests, not the
  code.
- **Process deviation, Task 8:** the agent wrote the implementation before the
  tests, so there was no red phase. Compensated with a mutation check
  (`DECAY` 0.8 -> 0.9 made 12 tests fail, including the exit test), reverted.
- No functional application bug was found by the smoke test.

## Known limitations / deferred
Carried from Phase 4 (unchanged): rollup look-ahead and staleness, no
automated partition creation, throughput-ceiling rerun, LoadGenerator ramp-up,
Postgres reconnect in the pipeline, commit-per-transaction batching.

New in Phase 5:
- **`since_id` cursor assumes a single writer** (commit order == id order).
  Documented in `query_since_id`, the `list_cases` docstring, and the `ws.py`
  module docstring. `LoadGenerator` running alongside the pipeline would
  violate it.
- **WebSocket slow-consumer bound is by message count, not bytes.** The
  agent found a `websockets` client that never reads did not back-pressure the
  server even at 100 MB of pushes; only a raw non-reading socket triggered
  eviction. Real slow browsers may be evicted later than expected. Needs a
  byte/size or send-timeout bound before production.
- **Redis pub/sub is at-most-once.** By design, REST repair is the source of
  truth. A dead Redis can stall the pipeline up to ~1 s per flagged case
  (publisher timeouts) but never fails detection.
- **`LoadGenerator` `full_pipeline` cases carry `rules_config_version = NULL`**
  (it passes a plain dict, wrapped in a static provider). Same as pre-Phase-5
  rows; not fixed to keep the benchmark tool minimal.
- **`threshold_count` upper bound uses the default `row_cap` (100).** A
  pipeline started with a smaller `--row-cap` could still have a velocity
  threshold that can never fire.
- **One extra SELECT + commit per newly flagged case** (to build the event
  summary). Could be folded into `INSERT ... RETURNING *`. Not done.
- **Priority combiner is duplicated** in `priority.py` (the detection engine is
  frozen). A seeded 500-case parity test asserts exact equality with
  `score_transaction`; a refactor to share one combiner is possible later.
- **No time decay or expiry** on old false-positive marks in demotion.
- **Tests share Redis/DB with anything live.** Some tests publish on the real
  `cases:events`/`rules:updated` channels and `CLIENT KILL TYPE pubsub` drops
  every pubsub connection on the shared Redis; a live API/pipeline on the dev
  Redis would see them. Serial pytest only, no xdist.
- **`rules_config_history` sequence advances** on every PATCH and is not
  restored by test teardown (versions like 308, 376 vs 4); rows are restored.
- **Phase 11 talking points:** `analyst` is unauthenticated free text (claim
  ownership and decision attribution are spoofable; any analyst can flip a
  verdict, visibly in the history).

## Key decisions & why
- Detection output immutable; analyst work and prioritization live in separate
  columns/tables (auditability).
- `flagged_cases.status` is a denormalized projection; the `case_feedback` log
  is authoritative and status is always recomputed, never written from the
  incoming value.
- `clock_timestamp()` plus a row lock for `decided_at` (`now()` is transaction
  start time and can invert against lock order).
- Demotion never suppresses: bounded by `FLOOR`, vetoed by any
  `confirmed_fraud` for the user (a false-positive mark can never silence a
  compromised account).
- Direct publish after commit (Option A) behind `publish_case_event()`,
  REST-repair as the delivery guarantee.

## Repo state at the September 21 handoff
- Branch `phase5-realtime-backend` was 23 commits ahead of
  `phase4-realtime-pipeline`. No orphan worktrees or branches remained.
- 249 backend tests passing; smoke 17/17.
- Docker: `sentinel-db` and `sentinel-redis` both must be up.
- Untracked (deliberately not committed):
  `docs/superpowers/plans/2026-09-21-phase5-full-code-report.md`, the
  mechanically generated full-code review file.

## Review follow-up — 2026-10-07
- Updated the README, which still described Phase 1 as unfinished, with the
  Phase 5 backend status and current setup and run commands.
- Rule PATCH validation now returns 422 for integers too large to fit the
  floating-point rule fields. Previously `math.isfinite` raised
  `OverflowError`, producing a server error. Two real-API regression cases
  cover the weight and parameter paths.
- Fresh main-worktree verification after both review fixes: **251 passed** in the full
  backend suite; the real API/pipeline smoke passed **17/17** steps and
  restored its database state. No frontend build exists yet.
- The veto-citation gap is resolved as described above; pure, database, and
  end-to-end exit tests now require the explanation while keeping priority
  unchanged. The spec's citation rule and exit criterion were updated.
- Draft PR #5 (`phase5-realtime-backend` into `phase4-realtime-pipeline`) is
  open for review. GitHub reports a clean merge state; the PR has no CI checks
  configured, so the verification above was run locally.

## Setup steps for a user (complete list, including pre-existing)
1. Start Docker Desktop; `docker compose -f infra/docker-compose.yml up -d db redis`.
   Check: `docker exec sentinel-db pg_isready -U sentinel -d sentinel` and
   `docker exec sentinel-redis redis-cli ping` (PONG).
2. Apply migrations 001-007 in order (001-006 are pre-existing requirements:
   transactions and partitions, rollup, `rules_config`, `flagged_cases`; 007 is
   new): `docker exec -i sentinel-db psql -U sentinel -d sentinel -v ON_ERROR_STOP=1 < infra/migrations/<file>.sql`.
   The Phase 1 dataset must be ingested (`scripts/ingest.py`).
3. `python3.12 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt`
   (now includes `redis==6.4.0`).
4. Optional env: `SENTINEL_DB_DSN` (default
   `postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel`),
   `REDIS_URL` (default `redis://localhost:6379/0`).
5. Run: API `cd backend && .venv/bin/python -m uvicorn app.main:app --port 8000`;
   pipeline `cd backend && .venv/bin/python -m app.pipeline.run_pipeline ...`;
   smoke `backend/.venv/bin/python scripts/smoke_phase5.py`.
