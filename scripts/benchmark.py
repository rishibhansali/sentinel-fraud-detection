"""Phase 1 'before' benchmark: naive per-user transaction history lookup.

Cold-cache methodology (locked in the Phase 1 design spec, not inferred
from idle time): restart the Postgres container immediately before the
single cold run, then run the warm runs back-to-back without restarting.
The restart reliably drops Postgres's own shared_buffers and forces a
fresh backend process, but it does not flush the host's OS-level page
cache — data files on the mounted volume can still be served from host
page cache across the restart. Column scope is locked to SELECT * in
both Phase 1 and Phase 2 so the index is the only variable that changes
between "before" and "after".
"""
import statistics
import subprocess
import time
from pathlib import Path

import psycopg2

DB_DSN = "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
COMPOSE_FILE = Path(__file__).parent.parent / "infra" / "docker-compose.yml"
BENCHMARK_USER_ID = 4885  # updated per Step 1's result (top user_id, 708 rows)
QUERY_LIMIT = 50
WARM_RUNS = 20
READINESS_TIMEOUT_S = 60

QUERY = "SELECT * FROM transactions WHERE user_id = %s ORDER BY ts DESC LIMIT %s;"


def restart_db_container() -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "restart", "db"], check=True
    )
    deadline = time.monotonic() + READINESS_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            conn = psycopg2.connect(DB_DSN, connect_timeout=2)
            conn.close()
            return
        except psycopg2.OperationalError:
            time.sleep(1)
    raise RuntimeError("Postgres did not become ready after restart")


def run_query_once() -> float:
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            start = time.perf_counter()
            cur.execute(QUERY, (BENCHMARK_USER_ID, QUERY_LIMIT))
            cur.fetchall()
            return time.perf_counter() - start
    finally:
        conn.close()


def main() -> None:
    print("Restarting Postgres container for cold-cache run...")
    restart_db_container()

    cold_seconds = run_query_once()
    print(f"Cold run: {cold_seconds * 1000:.2f} ms")

    warm_seconds = [run_query_once() for _ in range(WARM_RUNS)]
    warm_ms = sorted(s * 1000 for s in warm_seconds)
    median_ms = statistics.median(warm_ms)
    p95_ms = warm_ms[int(len(warm_ms) * 0.95) - 1]

    report = f"""# BENCHMARK.md

## Phase 1 — "before" baseline

Query (locked for apples-to-apples comparison with Phase 2 — column scope
does not change, only the index does):

```sql
{QUERY.strip()}
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
3. {WARM_RUNS} further queries run back-to-back afterward, without any
   restart — these are the recorded **warm** runs.

### Results

- Cold run: {cold_seconds * 1000:.2f} ms
- Warm runs (n={WARM_RUNS}): median {median_ms:.2f} ms, p95 {p95_ms:.2f} ms

Phase 2 appends its "after" numbers below this line using the same query
and the same restart-based cold-cache procedure.
"""
    (Path(__file__).parent.parent / "BENCHMARK.md").write_text(report)
    print(f"Warm median: {median_ms:.2f} ms, warm p95: {p95_ms:.2f} ms")
    print("Wrote BENCHMARK.md")


if __name__ == "__main__":
    main()
