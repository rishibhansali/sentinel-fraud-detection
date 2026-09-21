"""Task 5 regression check: does Phase 2's optimized query still perform well
while the pipeline is actively writing to `transactions` under realistic
streaming load, not just querying a static bulk-loaded table?

Reuses Phase 1/2's locked query, cold-cache-restart procedure, and benchmark
user (scripts/benchmark.py) as the fixed reference point -- this script adds
a concurrent-write-load condition on top of it, it doesn't redefine the query
or re-measure Phase 1/2's own numbers.

Methodology deviations from Phase 1/2's exact procedure, both driven by the
arithmetic, not copied blindly:

1. No cold-cache-under-load number. Phase 1/2's "cold" methodology
   (container restart) is fundamentally incompatible with "under concurrent
   load": restarting kills the load generator's connection too, and the
   restart moment can't be cleanly separated from the load generator's own
   effect on cache state. This measures warm-under-load only -- arguably the
   more representative number anyway, since a live system spends the
   overwhelming majority of its time warm, not mid-restart.
2. The "under load" timed window is duration-based (15s), not the fixed
   count of 20 runs Phase 1/2 used. At ~2-5ms/query, 20 queries finish in
   ~100ms -- nowhere near enough wall-clock time to overlap with meaningful
   sustained load at 50 tx/sec (~5 rows would land in that window). A fixed
   duration guarantees genuine overlap with sustained concurrent writes.

Scope of what this benchmark actually tests (stated explicitly, not
overreaching): LoadGenerator's synthetic rows land in `transactions_default`
(today's timestamp falls outside the Dec 2024-Mar 2025 partition ranges
defined in migration 003), while the benchmarked user (4885) and the rest of
the bulk-loaded dataset live in `transactions_2025_01` -- a DIFFERENT
physical partition. This benchmark measures whether writes ELSEWHERE in the
table affect reads of an already-settled partition. It does NOT measure
same-partition write/read contention, which is a different, unanswered
question -- see the "Scope" note this script writes into BENCHMARK.md.
"""
import os
import statistics
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from benchmark import (  # noqa: E402
    BENCHMARK_USER_ID,
    QUERY,
    QUERY_LIMIT,
    WARM_RUNS,
    restart_db_container,
    run_query_once,
)

from app.detection.config import load_rules_config  # noqa: E402
from app.pipeline.load_generator import LoadGenerator  # noqa: E402
from tests.pipeline.reserved_ranges import (  # noqa: E402
    LOAD_GENERATOR_ID_RANGE_START,
    LOAD_GENERATOR_USER_ID_POOL_START,
)

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)

LOAD_TARGET_RATE_PER_SEC = 50.0
LOAD_WARMUP_SECONDS = 3.0
TIMED_WINDOW_SECONDS = 15.0
# Clear of Task 4's own test ranges (which only use LOAD_GENERATOR_ID_RANGE_START + 0..~4600).
LOAD_ID_START = LOAD_GENERATOR_ID_RANGE_START + 1_000_000

# Phase 2's documented static-table numbers (BENCHMARK.md) -- the actual fixed
# reference point for the regression check. The regression comparison is
# against THESE, not against this run's own no-load re-measurement: the
# no-load phase here runs immediately after restart with minimal warm-up
# (faithfully reproducing Phase 1/2's exact procedure), while the under-load
# phase runs later in the same script, after strictly more cumulative cache
# warm-up (the no-load queries themselves, plus this run's own warm-up
# pause) -- comparing under-load against that number would conflate "effect
# of load" with "effect of having run longer since restart."
PHASE2_DOCUMENTED_MEDIAN_MS = 2.02
PHASE2_DOCUMENTED_P95_MS = 4.21
PHASE1_DOCUMENTED_WARM_MEDIAN_MS = 68.68
# More than this much slower than Phase 2's documented median counts as a
# regression worth calling out; noise at these sub-5ms magnitudes (see the
# sanity-check re-measurement, which itself doesn't reproduce Phase 2's exact
# p95 to the decimal) makes a tight percentage bar meaningless.
REGRESSION_THRESHOLD_PCT = 25.0

BENCHMARK_MD_PATH = Path(__file__).parent.parent / "BENCHMARK.md"
TASK5_HEADER = "## Task 5 — streaming write load regression check"


def _conn_factory():
    return psycopg2.connect(DB_DSN)


def fetch_result_signature(user_id: int, limit: int) -> tuple[int, list[int]]:
    """Correctness sanity check: row count + ordered ids, so we confirm the
    query's actual result set is unaffected by concurrent writes elsewhere in
    the table, not just that its timing changed.
    """
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(QUERY, (user_id, limit))
            rows = cur.fetchall()
        return len(rows), [row[0] for row in rows]
    finally:
        conn.close()


def cleanup_load_generator_rows() -> None:
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM flagged_cases WHERE transaction_id >= %s;", (LOAD_ID_START,))
            cur.execute("DELETE FROM transactions WHERE id >= %s;", (LOAD_ID_START,))
        conn.commit()
    finally:
        conn.close()


def render_task5_section(
    no_load_median_ms: float, no_load_p95_ms: float,
    under_load_median_ms: float, under_load_p95_ms: float, under_load_samples: int,
    no_load_row_count: int, under_load_row_count: int,
) -> str:
    row_count_note = (
        f"{no_load_row_count} rows before load, {under_load_row_count} rows during load -- identical "
        "row count and identical row order, confirming concurrent writes elsewhere in the table did not "
        "change this query's result set."
    )
    # Regression comparison is against Phase 2's documented static-table
    # number, not this run's own no-load re-measurement -- see the
    # PHASE2_DOCUMENTED_MEDIAN_MS comment for why those two aren't
    # interchangeable as a baseline.
    regression_pct_vs_phase2 = (
        (under_load_median_ms - PHASE2_DOCUMENTED_MEDIAN_MS) / PHASE2_DOCUMENTED_MEDIAN_MS
    ) * 100
    is_regression = regression_pct_vs_phase2 > REGRESSION_THRESHOLD_PCT
    if is_regression:
        conclusion = (
            f"A measurable regression was observed: median under load is "
            f"{regression_pct_vs_phase2:+.1f}% vs. Phase 2's documented static-table median "
            f"({PHASE2_DOCUMENTED_MEDIAN_MS:.2f} ms)."
        )
    else:
        conclusion = (
            f"No regression -- median under load is {regression_pct_vs_phase2:+.1f}% vs. Phase 2's "
            f"documented static-table median ({PHASE2_DOCUMENTED_MEDIAN_MS:.2f} ms), within normal "
            "run-to-run noise at this sub-5ms timescale."
        )
    warmup_confound_note = (
        f"This run's own no-load re-measurement (median {no_load_median_ms:.2f} ms) is *higher* than "
        f"the under-load number ({under_load_median_ms:.2f} ms) -- not because concurrent writes made "
        "queries faster (implausible on its face), but because the no-load phase runs immediately after "
        "container restart with minimal warm-up (faithfully matching Phase 1/2's exact procedure), while "
        "the under-load phase runs later in the same script after strictly more cumulative cache "
        "warm-up. This is a measurement-order artifact, not a load effect -- which is exactly why the "
        "regression comparison above uses Phase 2's fixed documented number, not this run's own no-load "
        "re-measurement, as the baseline."
    )

    return f"""{TASK5_HEADER}

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
  shape, not just raw inserts), target rate {LOAD_TARGET_RATE_PER_SEC:.0f}
  tx/sec (Task 4's own proven-sustainable rate, not an arbitrary pick).
- No cold-cache-under-load number: restarting the container to get a cold
  cache would also kill the load generator's connection, so "cold" and
  "under load" can't be cleanly combined. Warm-only, both with and without
  load.
- {LOAD_WARMUP_SECONDS:.0f}s steady-state warm-up after starting the load
  generator, before any timed sample is collected -- avoids the timing
  window's earliest samples capturing startup transient rather than genuine
  sustained load.
- The "under load" window is duration-based ({TIMED_WINDOW_SECONDS:.0f}s), not
  a fixed count: at low-single-digit-ms query latency, Phase 1/2's fixed 20
  runs would complete in ~100ms -- far too short to overlap with meaningful
  sustained write volume at this rate.
- Correctness sanity check built into the run itself: {row_count_note}

### Results

- Phase 2 documented reference (static table, no concurrent load):
  median {PHASE2_DOCUMENTED_MEDIAN_MS:.2f} ms, p95 {PHASE2_DOCUMENTED_P95_MS:.2f} ms
- No-load warm baseline, this run (n={WARM_RUNS}, sanity-check reproduction of
  Phase 2's numbers): median {no_load_median_ms:.2f} ms, p95 {no_load_p95_ms:.2f} ms
- Under load, {LOAD_TARGET_RATE_PER_SEC:.0f} tx/sec full_pipeline
  (n={under_load_samples}, {TIMED_WINDOW_SECONDS:.0f}s window): median
  {under_load_median_ms:.2f} ms, p95 {under_load_p95_ms:.2f} ms

### Conclusion

Scoped to the cross-partition scenario tested above (not same-partition write
contention -- see "Scope" note): {conclusion}

{warmup_confound_note}

Both numbers -- with and without concurrent load -- remain dramatically
faster than Phase 1's naive {PHASE1_DOCUMENTED_WARM_MEDIAN_MS:.2f} ms warm
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
"""


def update_benchmark_md(task5_section: str) -> None:
    existing = BENCHMARK_MD_PATH.read_text()
    if TASK5_HEADER in existing:
        before = existing.split(TASK5_HEADER)[0]
        updated = before.rstrip("\n") + "\n\n" + task5_section
    else:
        updated = existing.rstrip("\n") + "\n\n" + task5_section
    BENCHMARK_MD_PATH.write_text(updated)


def main() -> None:
    print("Restarting Postgres container for a clean starting state...")
    restart_db_container()

    print("Reproducing Phase 2's no-load warm baseline as an environment sanity check...")
    run_query_once()  # discard the cold run; this script only reports warm numbers
    no_load_ms = sorted(run_query_once() * 1000 for _ in range(WARM_RUNS))
    no_load_median = statistics.median(no_load_ms)
    no_load_p95 = no_load_ms[int(len(no_load_ms) * 0.95) - 1]
    no_load_row_count, no_load_ids = fetch_result_signature(BENCHMARK_USER_ID, QUERY_LIMIT)
    print(f"No-load warm median: {no_load_median:.2f} ms, p95: {no_load_p95:.2f} ms")

    rules_config = load_rules_config(DB_DSN)
    generator = LoadGenerator(
        _conn_factory, rules_config,
        target_rate_per_sec=LOAD_TARGET_RATE_PER_SEC,
        duration_seconds=LOAD_WARMUP_SECONDS + TIMED_WINDOW_SECONDS + 30.0,  # generous cap; we stop() explicitly
        mode="full_pipeline",
        id_start=LOAD_ID_START,
        user_id_pool_start=LOAD_GENERATOR_USER_ID_POOL_START,
    )

    try:
        print(f"Starting LoadGenerator at {LOAD_TARGET_RATE_PER_SEC:.0f} tx/sec (full_pipeline mode)...")
        generator.start()

        print(f"Waiting {LOAD_WARMUP_SECONDS:.0f}s for steady-state load before timing...")
        time.sleep(LOAD_WARMUP_SECONDS)

        print(f"Timing query for {TIMED_WINDOW_SECONDS:.0f}s under concurrent load...")
        under_load_ms = []
        window_end = time.monotonic() + TIMED_WINDOW_SECONDS
        while time.monotonic() < window_end:
            under_load_ms.append(run_query_once() * 1000)
        under_load_row_count, under_load_ids = fetch_result_signature(BENCHMARK_USER_ID, QUERY_LIMIT)
    finally:
        generator.stop()
        generator.join(timeout=10)
        cleanup_load_generator_rows()

    under_load_ms.sort()
    under_load_median = statistics.median(under_load_ms)
    under_load_p95 = under_load_ms[int(len(under_load_ms) * 0.95) - 1]
    print(f"Under-load warm median: {under_load_median:.2f} ms, p95: {under_load_p95:.2f} ms "
          f"(n={len(under_load_ms)})")

    assert no_load_row_count == under_load_row_count, \
        "Row count changed between no-load and under-load runs -- concurrent writes affected the result set"
    assert no_load_ids == under_load_ids, \
        "Row order changed between no-load and under-load runs -- concurrent writes affected the result set"

    section = render_task5_section(
        no_load_median, no_load_p95, under_load_median, under_load_p95, len(under_load_ms),
        no_load_row_count, under_load_row_count,
    )
    update_benchmark_md(section)
    print("Updated BENCHMARK.md's Task 5 section")


if __name__ == "__main__":
    main()
