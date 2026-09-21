"""Tests for LoadGenerator against the real running Postgres instance
(no mocks). This is a stress/throughput tool, not correctness logic (Tasks
1-3 already cover scoring correctness) -- these tests verify the generator
hits its target rate, distinguishes its two modes, stops promptly, honestly
detects falling behind schedule, and reports sane percentiles.
"""
import os
import time

import psycopg2
import pytest

from app.detection.config import load_rules_config
from app.pipeline.load_generator import LoadGenerator
from tests.pipeline.reserved_ranges import LOAD_GENERATOR_TEST_ID_BASE, LOAD_GENERATOR_USER_ID_POOL_START

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)


def _conn_factory():
    return psycopg2.connect(DB_DSN)


@pytest.fixture
def cleanup_transactions():
    ranges = []
    yield ranges
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            for start, end in ranges:
                cur.execute("DELETE FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s;", (start, end))
                cur.execute("DELETE FROM transactions WHERE id BETWEEN %s AND %s;", (start, end))
        conn.commit()
    finally:
        conn.close()


def test_full_pipeline_mode_achieves_approximately_target_rate(cleanup_transactions):
    id_start = LOAD_GENERATOR_TEST_ID_BASE
    cleanup_transactions.append((id_start, id_start + 999))
    rules_config = load_rules_config(DB_DSN)

    target_rate = 20.0
    max_transactions = 20

    generator = LoadGenerator(
        _conn_factory, rules_config,
        target_rate_per_sec=target_rate,
        duration_seconds=10.0,
        max_transactions=max_transactions,
        mode="full_pipeline",
        id_start=id_start,
        user_id_pool_start=LOAD_GENERATOR_USER_ID_POOL_START,
    )
    generator.start()
    generator.join(timeout=10)

    report = generator.report()
    assert report.total_transactions == max_transactions
    assert report.achieved_rate_per_sec == pytest.approx(target_rate, rel=0.3)

    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM transactions WHERE id BETWEEN %s AND %s;",
                        (id_start, id_start + 999))
            assert cur.fetchone()[0] == max_transactions
    finally:
        conn.close()


def test_insert_only_produces_no_flagged_cases_while_full_pipeline_does(cleanup_transactions):
    rules_config = load_rules_config(DB_DSN)

    insert_only_start = LOAD_GENERATOR_TEST_ID_BASE + 1000
    cleanup_transactions.append((insert_only_start, insert_only_start + 99))
    insert_only_gen = LoadGenerator(
        _conn_factory, rules_config,
        target_rate_per_sec=50.0,
        duration_seconds=10.0,
        max_transactions=15,
        mode="insert_only",
        user_pool_size=2,  # concentrate transactions on few users
        id_start=insert_only_start,
        user_id_pool_start=LOAD_GENERATOR_USER_ID_POOL_START,
    )
    insert_only_gen.start()
    insert_only_gen.join(timeout=10)

    full_pipeline_start = LOAD_GENERATOR_TEST_ID_BASE + 1200
    cleanup_transactions.append((full_pipeline_start, full_pipeline_start + 99))
    full_pipeline_gen = LoadGenerator(
        _conn_factory, rules_config,
        target_rate_per_sec=50.0,
        duration_seconds=10.0,
        max_transactions=15,
        mode="full_pipeline",
        user_pool_size=2,  # concentrate transactions -> velocity guaranteed to fire
        id_start=full_pipeline_start,
        user_id_pool_start=LOAD_GENERATOR_USER_ID_POOL_START - 10,  # distinct pool from insert_only run
    )
    full_pipeline_gen.start()
    full_pipeline_gen.join(timeout=10)

    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s;",
                        (insert_only_start, insert_only_start + 99))
            insert_only_flagged = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s;",
                        (full_pipeline_start, full_pipeline_start + 99))
            full_pipeline_flagged = cur.fetchone()[0]
    finally:
        conn.close()

    assert insert_only_flagged == 0
    assert full_pipeline_flagged > 0


def test_stop_halts_promptly(cleanup_transactions):
    id_start = LOAD_GENERATOR_TEST_ID_BASE + 2000
    cleanup_transactions.append((id_start, id_start + 999))
    rules_config = load_rules_config(DB_DSN)

    generator = LoadGenerator(
        _conn_factory, rules_config,
        target_rate_per_sec=5.0,
        duration_seconds=30.0,  # long enough that stop() must fire mid-run
        mode="insert_only",
        id_start=id_start,
        user_id_pool_start=LOAD_GENERATOR_USER_ID_POOL_START,
    )
    generator.start()

    time.sleep(0.5)
    generator.stop()
    generator.join(timeout=2)

    report = generator.report()
    assert report.duration_seconds < 2.0
    assert 0 < report.total_transactions < 100


def test_falling_behind_is_detected_when_target_rate_exceeds_capacity(cleanup_transactions):
    id_start = LOAD_GENERATOR_TEST_ID_BASE + 3000
    cleanup_transactions.append((id_start, id_start + 999))
    rules_config = load_rules_config(DB_DSN)

    generator = LoadGenerator(
        _conn_factory, rules_config,
        target_rate_per_sec=100_000.0,  # no real Postgres round-trip keeps up with this
        duration_seconds=1.0,
        max_transactions=500,  # bounds id usage regardless of test-machine throughput
        mode="insert_only",
        id_start=id_start,
        user_id_pool_start=LOAD_GENERATOR_USER_ID_POOL_START,
    )
    generator.start()
    generator.join(timeout=10)

    report = generator.report()
    assert report.fell_behind_at_transaction_index is not None
    assert report.fell_behind_at_seconds is not None
    assert report.achieved_rate_per_sec < report.target_rate_per_sec


def test_single_slow_transaction_does_not_falsely_trip_fell_behind(cleanup_transactions):
    """Regression test for the consecutive-miss fix: a lone slow transaction
    (a stand-in for a cold cache or one GC pause) must not permanently latch
    fell_behind_at_index, since the absolute-anchor schedule lets a one-time
    blip's backlog get paid down over the next couple of iterations rather
    than persisting. Same technique as Task 2's single-slow-callback drift
    test: patch in exactly one artificially slow operation among otherwise
    on-schedule ones and confirm the false-positive doesn't fire.

    target_rate=10/sec (interval=0.1s) is chosen deliberately low relative to
    real per-transaction insert cost (~1-2ms measured in Task 4's own
    numbers). The injected 0.3s blip (3x the interval) is large enough to
    register as a genuine miss at all -- a deficit has to exceed one full
    interval to count as a miss in the first place, so a smaller blip
    wouldn't even trigger the bug this test guards against -- but small
    enough that paying it down takes only ~2 non-sleeping iterations
    (confirmed by simulation: misses at indices 5 and 6, then recovery),
    comfortably under the CONSECUTIVE_MISS_THRESHOLD of 5.
    """
    id_start = LOAD_GENERATOR_TEST_ID_BASE + 4500
    cleanup_transactions.append((id_start, id_start + 99))
    rules_config = load_rules_config(DB_DSN)

    generator = LoadGenerator(
        _conn_factory, rules_config,
        target_rate_per_sec=10.0,
        duration_seconds=10.0,
        max_transactions=15,
        mode="insert_only",
        id_start=id_start,
        user_id_pool_start=LOAD_GENERATOR_USER_ID_POOL_START,
    )

    original_insert = generator._insert
    call_count = {"n": 0}

    def slow_on_fifth_call(conn, txn):
        call_count["n"] += 1
        if call_count["n"] == 5:
            time.sleep(0.3)  # one deliberate blip, 3x the 0.1s interval
        original_insert(conn, txn)

    generator._insert = slow_on_fifth_call

    generator.start()
    generator.join(timeout=10)

    report = generator.report()
    assert report.total_transactions == 15
    assert report.fell_behind_at_transaction_index is None
    assert report.fell_behind_at_seconds is None


def test_report_latency_percentiles_are_populated_and_monotonic(cleanup_transactions):
    id_start = LOAD_GENERATOR_TEST_ID_BASE + 4000
    cleanup_transactions.append((id_start, id_start + 999))
    rules_config = load_rules_config(DB_DSN)

    generator = LoadGenerator(
        _conn_factory, rules_config,
        target_rate_per_sec=30.0,
        duration_seconds=10.0,
        max_transactions=20,
        mode="full_pipeline",
        id_start=id_start,
        user_id_pool_start=LOAD_GENERATOR_USER_ID_POOL_START,
    )
    generator.start()
    generator.join(timeout=10)

    report = generator.report()
    assert report.latency_p50_ms > 0
    assert report.latency_p50_ms <= report.latency_p95_ms
    assert report.latency_p95_ms <= report.latency_p99_ms
    assert report.latency_p99_ms <= report.latency_max_ms
