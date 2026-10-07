"""Integration: GET /cases/{id} exposes the priority fields for a case that was
persisted by the REAL pipeline callback (not a hand-inserted row) with a
priority demotion in effect. Real Postgres, no mocks. Cleans up only its own
reserved range (feedback, cases, then transactions).
"""
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.cases.repository import claim_case, submit_feedback
from app.detection.config import load_rules_config
from app.detection.models import Transaction
from app.main import app
from app.pipeline.pipeline import make_pipeline_callback
from tests.pipeline.reserved_ranges import (
    INTEGRATION_TEST_TXN_BASE,
    INTEGRATION_TEST_TXN_MAX,
    INTEGRATION_TEST_USER_ID,
)

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)
TS = datetime(2025, 6, 20, 0, 0, 0, tzinfo=timezone.utc)
BASE = INTEGRATION_TEST_TXN_BASE
USER = INTEGRATION_TEST_USER_ID


def _cleanup(conn):
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM case_feedback WHERE case_id IN "
            "(SELECT id FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s)",
            (INTEGRATION_TEST_TXN_BASE, INTEGRATION_TEST_TXN_MAX),
        )
        cur.execute("DELETE FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s",
                    (INTEGRATION_TEST_TXN_BASE, INTEGRATION_TEST_TXN_MAX))
        cur.execute("DELETE FROM transactions WHERE id BETWEEN %s AND %s",
                    (INTEGRATION_TEST_TXN_BASE, INTEGRATION_TEST_TXN_MAX))
    conn.commit()


@pytest.fixture
def conn():
    c = psycopg2.connect(DB_DSN)
    _cleanup(c)
    try:
        yield c
    finally:
        _cleanup(c)
        c.close()


def _insert(conn, txn: Transaction):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO transactions (id, user_id, card_id, ts, amount, lat, lon,
                v1,v2,v3,v4,v5,v6,v7,v8,v9,v10,v11,v12,v13,v14,v15,v16,v17,v18,v19,v20,
                v21,v22,v23,v24,v25,v26,v27,v28, class, split)
               VALUES (%s,%s,1,%s,%s,%s,%s, 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,
                0,0,0,0,0,0,0,0, 0,'test')""",
            (txn.id, txn.user_id, txn.ts, txn.amount, txn.lat, txn.lon),
        )
    conn.commit()


def test_detail_returns_priority_fields_for_demoted_case(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT max(id) FROM rules_config_history")
        version = cur.fetchone()[0]
    rules = load_rules_config(DB_DSN)
    threshold = rules["velocity"].params["threshold_count"]
    window = rules["velocity"].params["window_minutes"]
    assert threshold * 10 < window * 60 * 3  # sanity: the burst below fits the window

    # Two bursts of `threshold` txns each (all inside the window, same coords,
    # modest equal amounts -> only velocity can fire; the user has no rollup
    # row so amount_baseline cannot fire either). The last txn of each burst
    # flags; the second flag must be demoted by the first's false_positive.
    txns = [
        Transaction(id=BASE + i, user_id=USER, ts=TS + timedelta(seconds=10 * i),
                    amount=10.0, lat=40.7, lon=-74.0)
        for i in range(threshold + 1)
    ]
    for t in txns:
        _insert(conn, t)

    from app.pipeline.rules_provider import RulesProvider
    provider = RulesProvider.static(rules, version=version)
    process = make_pipeline_callback(conn, provider)

    for t in txns[:threshold]:
        process(t)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM flagged_cases WHERE user_id = %s", (USER,))
        first_ids = [r[0] for r in cur.fetchall()]
    assert len(first_ids) == 1, "exactly the threshold-th txn should flag"
    first_id = first_ids[0]

    claim_case(conn, first_id, "integration")
    submit_feedback(conn, first_id, "integration", "false_positive", None)

    process(txns[threshold])  # flags again, now with a demotion in effect
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM flagged_cases WHERE user_id = %s AND id <> %s", (USER, first_id))
        second_id = cur.fetchone()[0]

    resp = TestClient(app).get(f"/cases/{second_id}")
    assert resp.status_code == 200
    d = resp.json()
    assert d["priority_score"] < d["total_score"]
    assert d["rules_config_version"] == version
    assert d["ml_anomaly_score"] is None
    adj = d["priority_adjustment"]
    assert adj is not None
    assert adj["veto"] is False
    (per_rule,) = adj["per_rule"]
    assert per_rule["rule_name"] == "velocity"
    assert per_rule["prior_false_positive_case_ids"] == [first_id]
    assert per_rule["prior_false_positive_count"] == 1

    # the first (undemoted) case has no adjustment and priority == total
    d1 = TestClient(app).get(f"/cases/{first_id}").json()
    assert d1["priority_adjustment"] is None
    assert d1["priority_score"] == d1["total_score"]
