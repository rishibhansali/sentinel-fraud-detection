# Sentinel Phase 1 — Baseline Design

## Purpose

Phase 1 of the Sentinel portfolio project (real-time fraud detection and
reviewer platform). Goal: get a real dataset flowing into a deliberately
naive Postgres schema, and produce an honest, reproducible "before" timing
for the query Phase 2 will optimize. No AI layer, no scoring logic, no
real-time pipeline — those are later phases. This spec covers only what's
listed as in-scope for Phase 1.

## Repo

- Name: `sentinel-fraud-detection`, public, under GitHub account
  `rishibhansali` (confirmed — single account in `gh auth status`).
- MIT license, Python+Node `.gitignore` (with `data/` raw files ignored),
  short initial README (project description, "no AI layer, still works"
  principle, planned stack, `Status: Phase 1 in progress`).

## Monorepo layout

```
sentinel/
  backend/        # FastAPI, requirements.txt-based
  frontend/        # placeholder only
  infra/           # docker-compose.yml (Postgres 16)
  data/            # gitignored raw + processed dataset
  scripts/         # ingest.py, benchmark.py
```

## Backend

Minimal FastAPI app, single `/health` endpoint returning `{"status": "ok"}`.
Dependency management: **requirements.txt** (not Poetry) — simplest path to
a working health check for a Phase 1 deliverable; no lockfile semantics
needed yet for a near-empty app. Revisit if the backend grows real
dependencies in later phases.

## Dataset: ULB Credit Card Fraud + synthetic augmentation

Source: ULB Credit Card Fraud Detection dataset (284,807 rows: `Time`,
`Amount`, `V1`–`V28` PCA features, `Class`). This dataset has **no**
`user_id`, `card_id`, or location fields — those are synthesized. The
`V1`–`V28`, `Amount`, and `Class` values are always the real, unmodified
values from the source row; only identity/geo fields are synthetic.

### Synthetic user_id / card_id assignment

No real identity signal exists in the source data, so any grouping is
inherently arbitrary. Heuristic (documented in code + `data/README.md`):

- `num_users = 5000`.
- `user_id = seeded_hash(row_index) % num_users` — deterministic and
  reproducible from a fixed seed, explicitly **not** derived from any
  pattern in `V1`–`V28`/`Amount` (there isn't one to derive from).
- Each user gets 1–2 `card_id`s (`card_id` derived from `user_id` plus a
  small deterministic offset), giving the schema a reason to carry both
  fields separately.

### Synthetic geo ("impossible geo" signal)

- Each synthetic user gets one seeded home `(lat, long)`.
- Per transaction: normally jitter tightly around home (Gaussian, ~10km
  std dev).
- Biased "impossible geo" fraction, keyed off the real `Class` label so
  the signal is actually detectable later without hand-building detection
  now: **~35% of `Class=1` rows** get a location randomly placed >500km
  from home; **~3% of `Class=0` rows** get the same treatment. Ratios are
  a documented judgment call, not derived from any external source.

### Volume expansion

Source is replicated **~11x to reach ~3.1M rows** (exact count = 11 ×
284,807 = 3,132,877, logged precisely by the ingest script at run time).
Each replica:

- Keeps the original row's `V1`–`V28`, `Amount`, `Class` unchanged (this
  is what preserves the real fraud-rate proportions exactly).
- Gets a fresh transaction id, an independently re-rolled `user_id`/
  `card_id` assignment, a timestamp shifted into a distinct time window
  per replica (so replicas span a longer synthetic time range rather than
  colliding), and an independently-rolled geo point (per the rule above).

`scripts/ingest.py` performs the full augmentation + expansion + load in
one idempotent (truncate-and-reload) pass, and prints the final row count
and augmentation parameters used.

## Train/val/test split (leakage-safe by construction)

A stratified (on `Class`) 70/15/15 train/val/test split is assigned to
each of the 284,807 **original** rows, once, **before** the 11x
replication step. Every replica generated from a given original row
inherits that row's split assignment unchanged — it is never re-rolled
per replica. This is what prevents the same underlying transaction from
leaking across train and test wearing a different synthetic identity in
a later ML phase. Persisted as a `split` column (`'train' | 'val' |
'test'`) on `transactions`. The 70/15/15 ratio is a documented judgment
call. No ML training or feature engineering happens in Phase 1 — this
only reserves the split for later phases to consume.

## Schema

Single wide `transactions` table, deliberately minimally indexed (primary
key only — no secondary indexes, no partitioning). Columns: transaction
id (PK), user_id, card_id, timestamp, amount, lat, long, `v1`..`v28`,
fraud label (`class`), split assignment (`split`).

Migration approach: **raw SQL** file (`infra/migrations/001_init.sql`),
applied via `psql` — no migration framework. A single naive table doesn't
need Alembic's versioning machinery in Phase 1; simplicity favors
transparency for a reviewer reading the repo.

## "Before" benchmark

Query: `SELECT * FROM transactions WHERE user_id = ? ORDER BY timestamp
DESC LIMIT N` — "get user X's last N transactions." Chosen because it's
the exact shape Phase 3's per-user velocity/baseline rules will need, and
it's the query a missing `(user_id, timestamp)` index hurts most directly
— an honest, representative naive-schema pain point rather than a
strawman.

Run against the full ~3.1M row table (not a sample). Multiple runs (e.g.
20+), report median and p95 latency. Results written to `BENCHMARK.md` at
repo root, which Phase 2 will append "after" numbers to.

**Column scope is locked: `SELECT *`, in both Phase 1 and Phase 2.** The
comparison must isolate the index as the only variable that changes
between "before" and "after" — narrowing columns in Phase 2 would mix
"added an index" with "reduced payload size" and invalidate the
comparison. Phase 2 must not quietly change this.

**Cold-cache methodology is explicit, not inferred from idle time:**

1. `docker compose restart db` (Postgres container restart — this is
   the cold-cache trigger, since it drops the OS page cache and Postgres
   shared_buffers backing the running container).
2. Run 1 query immediately after the restart completes and the container
   is accepting connections — this is the recorded **cold** run.
3. Without restarting again, run the remaining N warm runs back-to-back
   — these are the recorded **warm** runs, from which median/p95 are
   computed.

`scripts/benchmark.py` implements this restart step itself (shelling out
to `docker compose restart db` and waiting for readiness) rather than
relying on the operator's manual timing or on elapsed idle time as a
proxy for cache state. `BENCHMARK.md` states this exact procedure
alongside the numbers so the methodology is reproducible, not just the
result.

## Explicitly out of scope

Indexing/partitioning/rollups, scoring/rules logic, streaming ingestion,
WebSocket/API beyond `/health`, any Claude/AI integration, real frontend,
deployment/CI — all later phases.
