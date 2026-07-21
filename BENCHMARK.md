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

1. `docker compose -f infra/docker-compose.yml restart db` — drops the
   container's OS page cache and Postgres shared_buffers.
2. The first query after the container reports ready is the recorded
   **cold** run.
3. 20 further queries run back-to-back afterward, without any
   restart — these are the recorded **warm** runs.

### Results

- Cold run: 76.26 ms
- Warm runs (n=20): median 68.68 ms, p95 72.87 ms

Phase 2 appends its "after" numbers below this line using the same query
and the same restart-based cold-cache procedure.
