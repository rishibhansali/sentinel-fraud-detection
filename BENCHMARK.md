# BENCHMARK.md

## Phase 1 — "before" baseline

Query (locked for apples-to-apples comparison with Phase 2 — column scope
does not change, only the index does):

```sql
SELECT * FROM transactions WHERE user_id = %s ORDER BY ts DESC LIMIT %s;
```

Run against the full augmented `transactions` table (~3.1M rows; see
`data/README.md` for exact row count and augmentation method). Schema has
no secondary index on `user_id`/`ts` — primary key only (see
`infra/migrations/001_init.sql`).

### Methodology

Cold cache is triggered explicitly, not inferred from idle time:

1. `docker compose -f infra/docker-compose.yml restart db` — reliably
   drops Postgres's own `shared_buffers` and forces a fresh backend
   process. This does **not** flush the host's OS-level page cache;
   data files on the mounted volume can still be served from host page
   cache across the restart.
2. The first query after the container reports ready is the recorded
   **cold** run.
3. 20 further queries run back-to-back afterward, without any
   restart — these are the recorded **warm** runs.

### Results

- Cold run: 76.26 ms
- Warm runs (n=20): median 68.68 ms, p95 72.87 ms

## Phase 2 — "after" optimized

Same query, same `SELECT *` column scope, same restart-based cold-cache
procedure as Phase 1 — run once against the fully optimized schema: a
composite `(user_id, ts DESC)` index, monthly range partitioning on
`ts` (primary key now `(id, ts)`), and a `user_transaction_rollup`
materialized view (not used by this query directly — see "Why this
works" below).

### Results

- Cold run: 4.52 ms
- Warm runs (n=20): median 2.02 ms, p95 4.21 ms

### Why this works

- **Composite index `(user_id, ts DESC)`:** turns the query from a full
  sequential scan of ~3.1M rows into an index range scan that walks
  only one user's rows, already stored in the index in the exact order
  the query wants. `ORDER BY ts DESC LIMIT N` can stop as soon as it
  has N rows instead of reading and sorting the whole table — this is
  the entire reason the "after" number is dramatically smaller than
  Phase 1's baseline.
- **Monthly partitioning on `ts`:** contributes nothing to *this*
  query, for two independent reasons. First, the query doesn't filter
  on `ts` at all, so there's no time range for Postgres to prune
  partitions against — every partition is still a candidate. Second,
  even if the query did filter by time, it wouldn't matter for this
  dataset today: Phase 1's synthetic timestamps only span about 23
  days, so essentially all 3.1M rows sit inside the single
  `transactions_2025_01` partition regardless. Partitioning is
  architectural investment for needs this query doesn't have: bounding
  vacuum/maintenance cost per partition instead of over one
  ever-growing table, making old-data retention a cheap `DROP
  TABLE ... PARTITION` instead of a slow `DELETE`, and giving Phase 4's
  streaming ingest a small, recent partition to write into instead of
  one table that grows without bound.
- **Materialized view `user_transaction_rollup`:** doesn't touch this
  query's plan at all — it serves a different access pattern entirely
  (a precomputed per-user summary, refreshed on a schedule rather than
  computed live on every read) and is included in this phase because
  its aggregation logic (`user_transaction_features`) is the reusable
  building block Phase 6's feature engineering will need, kept isolated
  in its own view rather than buried inline in application code.
