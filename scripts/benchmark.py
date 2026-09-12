"""Phase 1 'before' + Phase 2 'after' benchmark: naive per-user
transaction history lookup, before and after indexing, partitioning,
and materialized-view optimizations.

Cold-cache methodology (locked from Phase 1, unchanged): restart the
Postgres container immediately before the single cold run, then run the
warm runs back-to-back without restarting. This drops Postgres's own
shared_buffers, not the host's OS-level page cache. Column scope is
locked to SELECT * in both phases so the index/partition changes are
the only variable that changes between "before" and "after".

This script only ever touches the "## Phase 2" section of BENCHMARK.md —
Phase 1's section is never regenerated or modified.
"""
import os
import statistics
import subprocess
import time
from pathlib import Path

import psycopg2

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)
COMPOSE_FILE = Path(__file__).parent.parent / "infra" / "docker-compose.yml"
BENCHMARK_USER_ID = 4885  # top user_id by row count, confirmed in Phase 1 (708 rows)
QUERY_LIMIT = 50
WARM_RUNS = 20
READINESS_TIMEOUT_S = 60

QUERY = "SELECT * FROM transactions WHERE user_id = %s ORDER BY ts DESC LIMIT %s;"

PHASE2_HEADER = '## Phase 2 — "after" optimized'
PHASE1_PLACEHOLDER = (
    'Phase 2 appends its "after" numbers below this line using the '
    'same query\nand the same restart-based cold-cache procedure.\n'
)
WHY_THIS_WORKS_MARKER = "### Why this works"


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


def render_phase2_section(cold_ms: float, median_ms: float, p95_ms: float) -> str:
    return f"""{PHASE2_HEADER}

Same query, same `SELECT *` column scope, same restart-based cold-cache
procedure as Phase 1 — run once against the fully optimized schema: a
composite `(user_id, ts DESC)` index, monthly range partitioning on
`ts` (primary key now `(id, ts)`), and a `user_transaction_rollup`
materialized view (not used by this query directly — see "Why this
works" below).

### Results

- Cold run: {cold_ms:.2f} ms
- Warm runs (n={WARM_RUNS}): median {median_ms:.2f} ms, p95 {p95_ms:.2f} ms
"""


def update_benchmark_md(phase2_section: str) -> None:
    benchmark_path = Path(__file__).parent.parent / "BENCHMARK.md"
    existing = benchmark_path.read_text()

    if WHY_THIS_WORKS_MARKER in existing:
        # Re-run: replace the Phase 2 numbers, but preserve any hand-written
        # explanation appended after them (e.g. Task 5's "Why this works"
        # section), which lives inside the "## Phase 2" section and would
        # otherwise be discarded along with the stale numbers.
        before_phase2 = (
            existing.split(PHASE2_HEADER)[0]
            if PHASE2_HEADER in existing
            else existing.rstrip("\n") + "\n\n"
        )
        preserved_tail = WHY_THIS_WORKS_MARKER + existing.split(WHY_THIS_WORKS_MARKER, 1)[1]
        updated = (
            before_phase2.rstrip("\n")
            + "\n\n"
            + phase2_section.rstrip("\n")
            + "\n\n"
            + preserved_tail
        )
    elif PHASE2_HEADER in existing:
        # Re-run: replace everything from the Phase 2 header onward.
        before = existing.split(PHASE2_HEADER)[0]
        updated = before.rstrip("\n") + "\n\n" + phase2_section
    elif PHASE1_PLACEHOLDER in existing:
        updated = existing.replace(PHASE1_PLACEHOLDER, phase2_section)
    else:
        updated = existing.rstrip("\n") + "\n\n" + phase2_section

    benchmark_path.write_text(updated)


def main() -> None:
    print("Restarting Postgres container for cold-cache run...")
    restart_db_container()

    cold_seconds = run_query_once()
    print(f"Cold run: {cold_seconds * 1000:.2f} ms")

    warm_seconds = [run_query_once() for _ in range(WARM_RUNS)]
    warm_ms = sorted(s * 1000 for s in warm_seconds)
    median_ms = statistics.median(warm_ms)
    p95_ms = warm_ms[int(len(warm_ms) * 0.95) - 1]

    update_benchmark_md(render_phase2_section(cold_seconds * 1000, median_ms, p95_ms))
    print(f"Warm median: {median_ms:.2f} ms, warm p95: {p95_ms:.2f} ms")
    print("Updated BENCHMARK.md's Phase 2 section")


if __name__ == "__main__":
    main()
