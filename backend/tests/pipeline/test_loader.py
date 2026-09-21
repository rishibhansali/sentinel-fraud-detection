"""Tests for get_recent_transactions()'s sort-order guarantee.

Tests 1-2 are pure (no DB) and prove *why* order matters, not just that a
list happens to be sorted. Tests 3-4 are integration tests against the real
running Postgres instance, matching this project's established no-mocks
pattern (see tests/detection/test_config.py).
"""
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from app.detection.models import RuleConfig, Transaction, UserBaseline
from app.detection.scoring import score_transaction
from app.pipeline.loader import UnsortedRecentTransactionsError, _assert_sorted_desc, get_recent_transactions

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)

TS = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)

# Reserved for test data only. Ingested dataset user_ids are in [0, 5000)
# (scripts/augment.py NUM_USERS) with positive auto-incrementing ids — negative
# values here can never collide with real rows, so tests can insert/delete
# freely without touching or depending on the ingested dataset.
TEST_USER_ID = -1001


def _config() -> dict[str, RuleConfig]:
    return {
        "velocity": RuleConfig("velocity", weight=1.0, enabled=True,
                                params={"window_minutes": 10, "threshold_count": 5}),
        "amount_baseline": RuleConfig("amount_baseline", weight=1.0, enabled=True,
                                       params={"deviation_multiplier": 3.0}),
        "geo_impossibility": RuleConfig("geo_impossibility", weight=1.0, enabled=True,
                                         params={"min_distance_km": 50.0, "max_speed_kmh": 900.0}),
    }


def _baseline() -> UserBaseline:
    return UserBaseline(transaction_count=10, avg_amount=100.0, total_amount=1000.0,
                         fraud_count=0, last_transaction_at=TS)


def test_assert_sorted_desc_raises_on_unsorted_input():
    unsorted = [
        Transaction(id=1, user_id=1, ts=TS - timedelta(minutes=5), amount=10.0, lat=0.0, lon=0.0),
        Transaction(id=2, user_id=1, ts=TS - timedelta(minutes=1), amount=10.0, lat=0.0, lon=0.0),
    ]

    with pytest.raises(UnsortedRecentTransactionsError):
        _assert_sorted_desc(unsorted)


def test_assert_sorted_desc_accepts_sorted_input():
    sorted_desc = [
        Transaction(id=2, user_id=1, ts=TS - timedelta(minutes=1), amount=10.0, lat=0.0, lon=0.0),
        Transaction(id=1, user_id=1, ts=TS - timedelta(minutes=5), amount=10.0, lat=0.0, lon=0.0),
    ]

    _assert_sorted_desc(sorted_desc)  # no raise


def test_transaction_order_changes_geo_impossibility_outcome():
    """Proves order is fraud-relevant: same two prior transactions, fed to
    score_transaction() in sorted-descending order vs. reversed order, produce
    different geo-impossibility outcomes. If get_recent_transactions ever lost
    its ORDER BY (or someone bypassed it), this is the difference it would
    silently cause in production, not just a cosmetic list-ordering bug.
    """
    txn = Transaction(id=99, user_id=1, ts=TS, amount=50.0, lat=LONDON[0], lon=LONDON[1])

    # Most recent prior transaction (id=2) is far away (NYC) and 1 minute ago:
    # implies an impossible speed -> geo-impossibility should fire.
    # Older prior transaction (id=1) is close to London and 4 minutes ago:
    # implies a plausible speed -> geo-impossibility should not fire.
    near_recent = Transaction(id=1, user_id=1, ts=TS - timedelta(minutes=4),
                               amount=10.0, lat=LONDON[0] + 0.01, lon=LONDON[1] + 0.01)
    far_recent = Transaction(id=2, user_id=1, ts=TS - timedelta(minutes=1),
                              amount=10.0, lat=NYC[0], lon=NYC[1])

    correctly_sorted = [far_recent, near_recent]  # descending by ts: id=2 then id=1
    reversed_order = [near_recent, far_recent]

    result_sorted = score_transaction(txn, correctly_sorted, _baseline(), _config())
    result_reversed = score_transaction(txn, reversed_order, _baseline(), _config())

    geo_sorted = next(r for r in result_sorted.rule_results if r.rule_name == "geo_impossibility")
    geo_reversed = next(r for r in result_reversed.rule_results if r.rule_name == "geo_impossibility")

    assert geo_sorted.fired is True
    assert geo_reversed.fired is False
    assert geo_sorted.fired != geo_reversed.fired


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(DB_DSN)
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE user_id = %s;", (TEST_USER_ID,))
        conn.commit()
        conn.close()


def _insert_transaction(conn, id_: int, user_id: int, ts: datetime, amount: float, lat: float, lon: float) -> None:
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
                %s, %s, 1, %s, %s, %s, %s,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0,
                0, 'test'
            );
            """,
            (id_, user_id, ts, amount, lat, lon),
        )
    conn.commit()


def test_get_recent_transactions_returns_real_rows_in_order(db_conn):
    _insert_transaction(db_conn, id_=-3001, user_id=TEST_USER_ID,
                         ts=TS - timedelta(minutes=9), amount=10.0, lat=0.0, lon=0.0)
    _insert_transaction(db_conn, id_=-3002, user_id=TEST_USER_ID,
                         ts=TS - timedelta(minutes=5), amount=20.0, lat=0.0, lon=0.0)
    _insert_transaction(db_conn, id_=-3003, user_id=TEST_USER_ID,
                         ts=TS - timedelta(minutes=1), amount=30.0, lat=0.0, lon=0.0)

    scored_id, scored_ts = -3004, TS

    result = get_recent_transactions(db_conn, TEST_USER_ID, scored_ts, scored_id, row_cap=100)

    assert [t.id for t in result] == [-3003, -3002, -3001]
    assert result[0].ts > result[1].ts > result[2].ts


def test_get_recent_transactions_same_timestamp_tiebreak_and_cutoff(db_conn):
    shared_ts = TS - timedelta(minutes=1)
    _insert_transaction(db_conn, id_=-3010, user_id=TEST_USER_ID,
                         ts=shared_ts, amount=10.0, lat=0.0, lon=0.0)
    _insert_transaction(db_conn, id_=-3011, user_id=TEST_USER_ID,
                         ts=shared_ts, amount=20.0, lat=0.0, lon=0.0)

    # The transaction being scored also shares the same ts but has a higher id
    # than both neighbors -- composite (ts, id) cutoff must exclude only this
    # one, not its identical-timestamp neighbors. -3009 > -3010 > -3011.
    scored_id, scored_ts = -3009, shared_ts

    result = get_recent_transactions(db_conn, TEST_USER_ID, scored_ts, scored_id, row_cap=100)

    assert [t.id for t in result] == [-3010, -3011]  # both neighbors present, id-tiebroken desc
    assert scored_id not in [t.id for t in result]
