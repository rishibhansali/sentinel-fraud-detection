import json
import os

import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.pipeline.reserved_ranges import (
    API_TEST_TXN_BASE,
    API_TEST_TXN_MAX,
    API_TEST_USER_ID,
)

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)

# Priority scores far above anything the real pipeline produces (0..1), so
# our rows sort to the very top of the shared queue and pages are predictable.
TOP = 1e9


def cleanup(c):
    with c.cursor() as cur:
        cur.execute(
            "DELETE FROM case_feedback WHERE case_id IN "
            "(SELECT id FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s)",
            (API_TEST_TXN_BASE, API_TEST_TXN_MAX),
        )
        cur.execute(
            "DELETE FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s",
            (API_TEST_TXN_BASE, API_TEST_TXN_MAX),
        )
    c.commit()


@pytest.fixture
def conn():
    c = psycopg2.connect(DB_DSN)
    cleanup(c)
    yield c
    c.rollback()
    cleanup(c)
    c.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def make_case(conn):
    counter = [0]

    def _make(status="open", claimed_by=None, priority=TOP, fired=(), unfired=()):
        counter[0] += 1
        rules = [
            {"rule_name": n, "fired": True, "sub_score": 0.9, "details": {}} for n in fired
        ] + [{"rule_name": n, "fired": False, "sub_score": 0.0, "details": {}} for n in unfired]
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO flagged_cases
                   (transaction_id, transaction_ts, user_id, total_score, rule_results,
                    priority_score, status, claimed_by, claimed_at)
                   VALUES (%s, '2025-01-01T00:00:00Z', %s, 0.5, %s, %s, %s, %s,
                           CASE WHEN %s::text IS NULL THEN NULL ELSE clock_timestamp() END)
                   RETURNING id""",
                (API_TEST_TXN_BASE + counter[0], API_TEST_USER_ID, json.dumps(rules),
                 priority, status, claimed_by, claimed_by),
            )
            cid = cur.fetchone()[0]
        conn.commit()
        return cid

    return _make
