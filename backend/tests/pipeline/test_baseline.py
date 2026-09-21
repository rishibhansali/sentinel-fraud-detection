"""Tests for get_user_baseline() against the real running Postgres instance.

REFRESH MATERIALIZED VIEW CONCURRENTLY over the ~3.1M-row dataset measured at
0.5-6.5s per call -- expensive enough that these tests call it exactly once,
only in the test that actually needs fresh rollup data.
"""
import os
from datetime import datetime, timezone

import psycopg2
import pytest

from app.detection.models import UserBaseline
from app.pipeline.loader import get_user_baseline

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)

TS = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

# Reserved, never written to by any test in this suite -- the miss case relies
# on this id never having a row in `transactions`, so `user_transaction_rollup`
# (GROUP BY user_id over that table) can never contain it, refreshed or not.
NEVER_SEEDED_USER_ID = -2099

# Reserved for the seeded-baseline test only.
SEEDED_USER_ID = -2001
SEEDED_TEST_ID_BASE = 6_000_000


def _insert_transaction(conn, id_: int, user_id: int, ts: datetime, amount: float) -> None:
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
                %s, %s, 1, %s, %s, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0,
                0, 'test'
            );
            """,
            (id_, user_id, ts, amount),
        )
    conn.commit()


def test_get_user_baseline_returns_zeroed_baseline_on_miss():
    conn = psycopg2.connect(DB_DSN)
    try:
        result = get_user_baseline(conn, NEVER_SEEDED_USER_ID)
    finally:
        conn.close()

    assert result == UserBaseline(transaction_count=0, avg_amount=0.0, total_amount=0.0,
                                   fraud_count=0, last_transaction_at=None)


def test_get_user_baseline_reads_seeded_rollup_row_after_refresh():
    conn = psycopg2.connect(DB_DSN)
    try:
        _insert_transaction(conn, SEEDED_TEST_ID_BASE, SEEDED_USER_ID, TS, amount=100.0)
        _insert_transaction(conn, SEEDED_TEST_ID_BASE + 1, SEEDED_USER_ID, TS, amount=200.0)
        _insert_transaction(conn, SEEDED_TEST_ID_BASE + 2, SEEDED_USER_ID, TS, amount=300.0)

        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY user_transaction_rollup;")
        conn.commit()

        result = get_user_baseline(conn, SEEDED_USER_ID)

        assert result.transaction_count == 3
        assert result.avg_amount == pytest.approx(200.0)
        assert result.total_amount == pytest.approx(600.0)
        assert result.fraud_count == 0
        assert result.last_transaction_at == TS
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE user_id = %s;", (SEEDED_USER_ID,))
        conn.commit()
        conn.close()
