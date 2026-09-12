# Sentinel Phase 2 — Query Optimization Design

## Purpose

Phase 2 fixes the slow, naive per-user lookup query Phase 1 deliberately
left unoptimized, and produces a documented, reproducible before/after
comparison. Query shape is locked from Phase 1 and does not change:

```sql
SELECT * FROM transactions WHERE user_id = ? ORDER BY ts DESC LIMIT N;
```

`SELECT *` and the restart-based cold-cache definition (drops Postgres
`shared_buffers`, not the OS page cache) are both locked from Phase 1 —
neither changes in this phase, and Phase 1's baseline numbers in
`BENCHMARK.md` are not re-measured.

## In scope

### 1. Composite index: `(user_id, ts DESC)`

`CREATE INDEX CONCURRENTLY idx_transactions_user_id_ts ON transactions
(user_id, ts DESC);` — the primary fix. Turns the query from a full
sequential scan into an index range scan that can stop after `LIMIT N`
rows instead of reading the whole table.

### 2. Monthly range partitioning on `ts`

Postgres has no in-place `ALTER TABLE ... PARTITION BY`; converting an
existing table requires a rebuild:

1. Rename `transactions` → `transactions_old`.
2. Create a new `transactions` table, identical columns,
   `PARTITION BY RANGE (ts)`. Primary key becomes `(id, ts)` — Postgres
   requires the partition key be part of any unique constraint. This is
   a no-op in practice (`id` was already globally unique) but is a real,
   confirmed schema change.
3. Create monthly partitions covering the actual data range plus
   padding, and one `DEFAULT` partition as a catch-all.
4. Re-declare the `(user_id, ts DESC)` index on the new partitioned
   parent (Postgres propagates it to every partition automatically).
5. Copy all rows from `transactions_old` into the new `transactions`
   (auto-routed by `ts`); verify row count, fraud rate, and split
   counts match exactly (same checks as Phase 1); drop `transactions_old`.

**Known, confirmed characteristic:** Phase 1's synthetic timestamps span
only ~23 days (Jan 1–23, 2025, an artifact of the replica time-shifting),
so with monthly partitions essentially all 3.1M rows land in a single
January partition today. Partitioning therefore prunes nothing for this
benchmark query for two independent reasons: the query doesn't filter by
time at all, and the current data wouldn't benefit even if it did. This
is documented explicitly rather than hidden — partitioning is being
built for architectural correctness (recent-window queries, write-side
maintenance/vacuum granularity, Phase 4's streaming ingest), not because
it moves this specific number.

### 3. Materialized view: per-user rollup

- `user_transaction_features` (plain view): `user_id`, `transaction_count`,
  `avg_amount`, `total_amount`, `fraud_count`, `last_transaction_at` — the
  single reusable source of truth for this aggregation, referenceable by
  Phase 6's feature engineering later rather than re-derived inline.
- `user_transaction_rollup` (materialized view): `SELECT * FROM
  user_transaction_features`, with a unique index on `user_id` so
  `REFRESH MATERIALIZED VIEW CONCURRENTLY` is supported (non-blocking).
- Refresh cadence: documented as every 5 minutes; only the manual/
  scriptable `REFRESH MATERIALIZED VIEW CONCURRENTLY user_transaction_rollup;`
  command is implemented in this phase. No cron/pg_cron scheduler is
  stood up — deferred, since no scheduling infrastructure exists yet in
  this project (confirmed decision, not an oversight).

### 4. `benchmark.py` updates

- Fix DSN handling to honor `SENTINEL_DB_DSN` env var, matching
  `ingest.py` (existing inconsistency, fixed as part of this phase).
- Same query, same `SELECT *`, same restart-based cold-cache trigger as
  Phase 1. Run once against the fully optimized end-state (index +
  partitions + view all in place).
- Append results under `BENCHMARK.md`'s existing "Phase 2" placeholder
  — Phase 1's numbers are not modified or re-measured.

### 5. Mechanical explanation

Appended to `BENCHMARK.md` (not a separate doc, to keep numbers and
mechanics together): why the index turns a seq scan into a range scan;
why partitioning contributes nothing to *this* query and why that's
still worth building; why the materialized view trades staleness for
read speed on a different access pattern than the benchmark query.
Written so it's usable as interview prep without notes.

## Explicitly out of scope

Any scoring/detection logic (Phase 3), streaming ingest (Phase 4),
WebSocket/API work (Phase 5), Claude integration (Phase 6), real
frontend (Phase 7), deployment/CI (Phase 8), and any actual
cron/pg_cron scheduler for the materialized view refresh (deferred,
confirmed).
