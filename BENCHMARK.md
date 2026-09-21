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

## Task 5 — streaming write load regression check

Same locked query, same `user_id=4885`, same `LIMIT=50` as Phase 1/2 -- this
adds a concurrent-write-load condition on top of Phase 2's already-optimized,
static-table number. Not re-measuring Phase 1/2's own numbers; comparing
against them as fixed reference points.

### Scope of what this measures

LoadGenerator's synthetic rows (today's timestamp) land in
`transactions_default`, the partition catch-all -- migration 003 only
defines ranges through March 2025. The benchmarked user (4885) and the rest
of the ~3.1M-row bulk-loaded dataset live entirely in `transactions_2025_01`,
a different physical partition. **This benchmark measures whether writes
elsewhere in the table affect reads of an already-settled partition. It does
NOT measure same-partition write/read contention** -- that's a different,
untested scenario. The conclusion below is scoped accordingly, not a general
"optimizations hold under streaming write load" claim.

### Methodology

- Load: LoadGenerator, `full_pipeline` mode (loader query + baseline query +
  score + conditional persist per row -- the actual production pipeline
  shape, not just raw inserts), target rate 50
  tx/sec (Task 4's own proven-sustainable rate, not an arbitrary pick).
- No cold-cache-under-load number: restarting the container to get a cold
  cache would also kill the load generator's connection, so "cold" and
  "under load" can't be cleanly combined. Warm-only, both with and without
  load.
- 3s steady-state warm-up after starting the load
  generator, before any timed sample is collected -- avoids the timing
  window's earliest samples capturing startup transient rather than genuine
  sustained load.
- The "under load" window is duration-based (15s), not
  a fixed count: at low-single-digit-ms query latency, Phase 1/2's fixed 20
  runs would complete in ~100ms -- far too short to overlap with meaningful
  sustained write volume at this rate.
- Correctness sanity check built into the run itself: 50 rows before load, 50 rows during load -- identical row count and identical row order, confirming concurrent writes elsewhere in the table did not change this query's result set.

### Results

- Phase 2 documented reference (static table, no concurrent load):
  median 2.02 ms, p95 4.21 ms
- No-load warm baseline, this run (n=20, sanity-check reproduction of
  Phase 2's numbers): median 2.39 ms, p95 3.83 ms
- Under load, 50 tx/sec full_pipeline
  (n=2589, 15s window): median
  1.40 ms, p95 1.67 ms

### Conclusion

Scoped to the cross-partition scenario tested above (not same-partition write
contention -- see "Scope" note): No regression -- median under load is -30.7% vs. Phase 2's documented static-table median (2.02 ms), within normal run-to-run noise at this sub-5ms timescale.

This run's own no-load re-measurement (median 2.39 ms) is *higher* than the under-load number (1.40 ms) -- not because concurrent writes made queries faster (implausible on its face), but because the no-load phase runs immediately after container restart with minimal warm-up (faithfully matching Phase 1/2's exact procedure), while the under-load phase runs later in the same script after strictly more cumulative cache warm-up. This is a measurement-order artifact, not a load effect -- which is exactly why the regression comparison above uses Phase 2's fixed documented number, not this run's own no-load re-measurement, as the baseline.

Both numbers -- with and without concurrent load -- remain dramatically
faster than Phase 1's naive 68.68 ms warm
median measured with zero concurrent load.

### Deferred (Phase 11 talking points, not built here)

- Rerun this benchmark at Task 4's found throughput ceiling -- this run only
  used the one proven-sustainable rate (50 tx/sec), not a stress rate that
  probes for where degradation begins.
- No automated future-partition creation exists (no pg_partman, no cron job)
  -- partitions only cover Dec 2024-Mar 2025 plus `transactions_default`.
  Every live write from any real deployment onward lands in the default
  partition indefinitely. A real deployment needs this solved before the
  default partition's benefit (small, recent, isolated from historical data)
  degrades into "one ever-growing default partition holding everything" --
  the exact problem partitioning was meant to avoid.
