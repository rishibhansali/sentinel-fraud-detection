"""Tests for ReplayHarness against the real running Postgres instance
(matching this project's no-mocks convention). Test rows use an id range
starting well above the real dataset's max id (3,132,877), so keyset
pagination on `id > start_id` never scans real dataset rows -- it lands
directly on the reserved test slice.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from app.detection.models import Transaction
from app.pipeline.replay import ReplayHarness

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)

TS = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

# Reserved test range: real dataset ids top out at 3,132,877. Starting at
# 4,000,000 guarantees no collision and no scan through real rows.
TEST_ID_BASE = 4_000_000


def _conn_factory():
    return psycopg2.connect(DB_DSN)


def _insert_transaction(conn, id_: int, ts: datetime, amount: float = 10.0) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (
                id, user_id, card_id, ts, amount, lat, lon,
                v1, v2, v3, v4, v5, v6, v7, v8, v9, v10,
                v11, v12, v13, v14, v15, v16, v17, v18, v19, v20,
                v21, v22, v23, v24, v25, v26, v27, v28,
                class, split
            ) VALUES (
                %s, 1, 1, %s, %s, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0,
                0, 'test'
            );
            """,
            (id_, ts, amount),
        )
    conn.commit()


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(DB_DSN)
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE id BETWEEN %s AND %s;", (TEST_ID_BASE, TEST_ID_BASE + 999))
        conn.commit()
        conn.close()


def test_delivers_transactions_in_id_order(db_conn):
    base = TEST_ID_BASE + 100
    for i in range(4):
        _insert_transaction(db_conn, id_=base + i, ts=TS + timedelta(seconds=i))

    received: list[Transaction] = []
    harness = ReplayHarness(_conn_factory, speed_multiplier=1000.0, start_id=base - 1, end_id=base + 3)
    harness.start(received.append)
    harness.join(timeout=5)

    assert [t.id for t in received] == [base, base + 1, base + 2, base + 3]


def test_paces_delivery_at_expected_intervals(db_conn):
    base = TEST_ID_BASE + 200
    gap_seconds = 1.0
    n = 4
    for i in range(n):
        _insert_transaction(db_conn, id_=base + i, ts=TS + timedelta(seconds=i * gap_seconds))

    speed_multiplier = 20.0  # 1s dataset gap -> 0.05s real gap
    arrival_times: list[float] = []

    def on_transaction(txn):
        arrival_times.append(time.monotonic())

    harness = ReplayHarness(_conn_factory, speed_multiplier=speed_multiplier,
                             start_id=base - 1, end_id=base + n - 1)
    harness.start(on_transaction)
    harness.join(timeout=5)

    assert len(arrival_times) == n
    expected_gap = gap_seconds / speed_multiplier  # 0.05s
    for i in range(1, n):
        actual_gap = arrival_times[i] - arrival_times[i - 1]
        assert actual_gap == pytest.approx(expected_gap, abs=0.05)


def test_stop_halts_promptly_and_delivers_no_more_after(db_conn):
    base = TEST_ID_BASE + 300
    n = 10
    gap_seconds = 5.0  # slow enough that stop() will fire mid-replay
    for i in range(n):
        _insert_transaction(db_conn, id_=base + i, ts=TS + timedelta(seconds=i * gap_seconds))

    received: list[Transaction] = []
    harness = ReplayHarness(_conn_factory, speed_multiplier=1.0, start_id=base - 1, end_id=base + n - 1)
    harness.start(received.append)

    time.sleep(0.2)
    harness.stop()
    harness.join(timeout=2)

    count_at_stop = len(received)
    assert 0 < count_at_stop < n  # stopped mid-replay, not at the end

    time.sleep(0.3)
    assert len(received) == count_at_stop  # nothing delivered after stop() returned


def test_higher_speed_multiplier_replays_faster(db_conn):
    base = TEST_ID_BASE + 400
    n = 5
    gap_seconds = 0.3
    for i in range(n):
        _insert_transaction(db_conn, id_=base + i, ts=TS + timedelta(seconds=i * gap_seconds))

    def time_replay(speed_multiplier: float) -> float:
        received = []
        harness = ReplayHarness(_conn_factory, speed_multiplier=speed_multiplier,
                                 start_id=base - 1, end_id=base + n - 1)
        start = time.monotonic()
        harness.start(received.append)
        harness.join(timeout=5)
        assert len(received) == n
        return time.monotonic() - start

    slow_elapsed = time_replay(speed_multiplier=5.0)
    fast_elapsed = time_replay(speed_multiplier=10.0)

    assert fast_elapsed < slow_elapsed * 0.75


def test_pacing_does_not_drift_when_callback_is_slow(db_conn):
    """The regression test for the absolute-anchor fix: without it, sleeping
    relative to the previous transaction's timestamp means callback time
    compounds on top of every gap, and total wall-clock time drifts further
    past the expected paced duration with every row. With the anchor fix,
    callback time only delays that row's own delivery -- it never
    accumulates -- so total wall-clock time stays close to the dataset's
    span (scaled by the multiplier), independent of how slow the callback is.
    """
    base = TEST_ID_BASE + 500
    n = 6
    gap_seconds = 0.2
    for i in range(n):
        _insert_transaction(db_conn, id_=base + i, ts=TS + timedelta(seconds=i * gap_seconds))

    speed_multiplier = 1.0
    span_seconds = (n - 1) * gap_seconds  # 1.0s
    callback_sleep = 0.12  # 60% of the 0.2s gap -- deliberately non-trivial

    def slow_on_transaction(txn):
        time.sleep(callback_sleep)

    harness = ReplayHarness(_conn_factory, speed_multiplier=speed_multiplier,
                             start_id=base - 1, end_id=base + n - 1)
    start = time.monotonic()
    harness.start(slow_on_transaction)
    harness.join(timeout=10)
    elapsed = time.monotonic() - start

    # Fixed (anchor-based) behavior: ~span + one trailing callback ~= 1.12s.
    # Buggy (relative-sleep) behavior would be ~= n * (gap + callback) = 1.92s.
    # Assert comfortably below the buggy value and reasonably close to the
    # fixed one -- proving time didn't compound with callback overhead.
    assert elapsed < span_seconds + callback_sleep + 0.3
    assert elapsed > span_seconds * 0.8
