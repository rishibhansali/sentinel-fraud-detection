"""Phase 5 Task 8 EXIT TEST (spec section 8): feedback demotes future flags.

REAL Postgres, REAL Redis, real code paths: the pipeline callback runs over
real `transactions` rows, and feedback is submitted through the real API
(`POST /cases/{id}/feedback` via FastAPI TestClient), never by editing rows.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import pytest
from fastapi.testclient import TestClient

from app.detection.config import load_rules_config
from app.detection.models import Transaction
from app.main import app
from app.pipeline.pipeline import make_pipeline_callback
from app.pipeline.rules_provider import RulesProvider
from app.realtime import publisher
from app.realtime.config import CASES_CHANNEL, redis_url
from app.realtime.subscriber import ThreadedChannelSubscriber
from tests.pipeline.reserved_ranges import PRIORITY_TEST_TXN_BASE, PRIORITY_TEST_USER_ID

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)
EXIT_BASE = PRIORITY_TEST_TXN_BASE + 5_000  # BASE+5000..BASE+5999 (unit tests use < BASE+3000)
EXIT_MAX = EXIT_BASE + 999
U, V, W = PRIORITY_TEST_USER_ID, PRIORITY_TEST_USER_ID - 1, PRIORITY_TEST_USER_ID - 2
T0 = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)
R = "geo_impossibility"


def _cleanup(c):
    with c.cursor() as cur:
        cur.execute(
            "DELETE FROM case_feedback WHERE case_id IN "
            "(SELECT id FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s)",
            (EXIT_BASE, EXIT_MAX),
        )
        cur.execute("DELETE FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s", (EXIT_BASE, EXIT_MAX))
        cur.execute("DELETE FROM transactions WHERE id BETWEEN %s AND %s", (EXIT_BASE, EXIT_MAX))
    c.commit()


@pytest.fixture
def conn():
    c = psycopg2.connect(DB_DSN)
    _cleanup(c)
    try:
        yield c
    finally:
        c.rollback()
        _cleanup(c)
        c.close()


def _add_txn(conn, id_, user, offset_s, loc):
    ts = T0 + timedelta(seconds=offset_s)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (id, user_id, card_id, ts, amount, lat, lon,
                v1, v2, v3, v4, v5, v6, v7, v8, v9, v10, v11, v12, v13, v14, v15, v16, v17, v18, v19, v20,
                v21, v22, v23, v24, v25, v26, v27, v28, class, split)
            VALUES (%s, %s, 1, %s, 10.0, %s, %s,
                0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0, 0, 'test')
            """,
            (id_, user, ts, *loc),
        )
    conn.commit()
    return Transaction(id=id_, user_id=user, ts=ts, amount=10.0, lat=loc[0], lon=loc[1])


def _case_for(txn):
    c = psycopg2.connect(DB_DSN)
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM flagged_cases WHERE transaction_id = %s", (txn.id,))
            return cur.fetchone()
    finally:
        c.close()


def _fired(row):
    return sorted(r["rule_name"] for r in row["rule_results"] if r["fired"])


def _show(label, row):
    print(
        f"    {label}: case_id={row['id']} user={row['user_id']} fired={_fired(row)} "
        f"total_score={row['total_score']:.6f} priority_score={row['priority_score']:.6f} "
        f"status={row['status']} adjustment={json.dumps(row['priority_adjustment'])}"
    )


def test_demotion_exit_criteria(conn):
    cfg = load_rules_config(DB_DSN)
    run = make_pipeline_callback(conn, RulesProvider.static(cfg, None))
    client = TestClient(app)
    publisher.reset_publish_failure_count()
    events: list = []
    sub = ThreadedChannelSubscriber(redis_url(), CASES_CHANNEL, events.append)
    sub.start()
    try:
        assert sub.wait_ready(5)
        _run_scenario(conn, run, client, events)
    finally:
        sub.stop()


def _run_scenario(conn, run, client, events):
    def flag(txn):
        run(txn)
        row = _case_for(txn)
        assert row is not None, f"transaction {txn.id} was NOT flagged (case suppressed or rule did not fire)"
        return row

    def feedback(case_id, decision):
        r = client.post(f"/cases/{case_id}/feedback", json={"analyst": "exit-test", "decision": decision})
        assert r.status_code == 200, r.text
        print(f"    POST /cases/{case_id}/feedback {decision} -> {r.status_code} status={r.json()['status']}")
        return r.json()

    def user_txns(user, idx):
        """Deterministic per-user timeline. idx offsets the txn ids."""
        b = EXIT_BASE + idx * 100
        return {
            "seed_nyc": _add_txn(conn, b + 0, user, 0, NYC),
            "london": _add_txn(conn, b + 1, user, 270, LONDON),  # geo fires (NYC->London in 270s)
            "nyc_again": _add_txn(conn, b + 2, user, 7200, NYC),  # geo fires (London->NYC in ~2h)
        }

    print("\n=== EXIT TEST: priority demotion (spec section 8) ===")

    # ---- Step 1: baseline
    print("STEP 1: baseline, user U fires rule R=geo_impossibility")
    u = user_txns(U, 0)
    a = flag(u["london"])
    _show("case A", a)
    assert R in _fired(a)
    assert a["priority_score"] == a["total_score"]
    assert a["priority_adjustment"] is None

    # ---- Step 2: false positive via API
    print("STEP 2: analyst marks case A false_positive via the API")
    body = feedback(a["id"], "false_positive")
    assert body["status"] == "false_positive"

    # ---- Control user V (step 5) runs the identical timeline, no feedback
    print("STEP 5 (control): user V, identical scenario, NO feedback")
    v = user_txns(V, 1)
    v1 = flag(v["london"])
    _show("V case 1", v1)
    v3 = flag(v["nyc_again"])
    _show("V case 2", v3)
    assert v1["priority_score"] == v1["total_score"] and v1["priority_adjustment"] is None
    assert v3["priority_score"] == v3["total_score"] and v3["priority_adjustment"] is None

    # ---- Step 3: new U flag of the same rule
    print("STEP 3: NEW transaction from U fires the SAME rule R")
    b = flag(u["nyc_again"])
    _show("case B", b)
    assert b["id"] != a["id"]  # a case was created, not suppressed
    assert R in _fired(b)
    assert b["priority_score"] < b["total_score"]
    assert b["total_score"] == v3["total_score"], "total_score must equal the no-feedback value (control V)"
    assert b["priority_score"] >= 0.5 * b["total_score"]
    adj = b["priority_adjustment"]
    assert adj["formula_version"] == 1 and adj["decay"] == 0.8 and adj["floor"] == 0.5 and adj["veto"] is False
    assert len(adj["per_rule"]) == 1
    per = adj["per_rule"][0]
    assert per["rule_name"] == R
    assert per["prior_false_positive_count"] == 1
    assert per["prior_false_positive_case_ids"] == [a["id"]]
    assert per["factor"] == 0.8
    print(f"    OK: total_score unchanged ({b['total_score']:.6f} == control {v3['total_score']:.6f}); "
          f"priority {b['priority_score']:.6f} in [{0.5 * b['total_score']:.6f}, {b['total_score']:.6f}); "
          f"citation names case {a['id']} count=1 factor=0.8")

    # published case.created carries the demoted priority (real Redis)
    deadline = time.time() + 5
    got = None
    while time.time() < deadline and got is None:
        for m in list(events):
            m = json.loads(m) if isinstance(m, (str, bytes)) else m
            if m.get("type") == "case.created" and m["case"]["id"] == b["id"]:
                got = m["case"]
        time.sleep(0.02)
    assert got is not None, "case.created was not published for the demoted case"
    assert got["priority_score"] == b["priority_score"] and got["total_score"] == b["total_score"]
    print(f"    OK: Redis case.created for case {b['id']} published with priority_score={got['priority_score']:.6f}")

    # ---- Step 4a: different user, same rule
    print("STEP 4a: DIFFERENT user W fires the same rule R")
    w = user_txns(W, 2)
    wc = flag(w["london"])
    _show("W case", wc)
    assert R in _fired(wc)
    assert wc["priority_score"] == wc["total_score"] and wc["priority_adjustment"] is None

    # ---- Step 4b: same user, different rule (velocity only; geo cannot fire: same location as prior)
    print("STEP 4b: user U fires a DIFFERENT rule (velocity) than R")
    b0 = EXIT_BASE + 50
    for i in range(4):
        _add_txn(conn, b0 + i, U, 14400 + 60 * i, NYC)
    c = flag(_add_txn(conn, b0 + 4, U, 14400 + 240, NYC))
    _show("case C", c)
    assert _fired(c) == ["velocity"]
    assert c["priority_score"] == c["total_score"] and c["priority_adjustment"] is None
    print("    OK: velocity factor 1.0 (no citation), unaffected by U's geo false positive")

    # ---- Step 6: confirmed_fraud for U vetoes all demotion
    print("STEP 6: analyst confirms fraud on another U case (C) via the API; next U/R flag")
    feedback(c["id"], "confirmed_fraud")
    d = flag(_add_txn(conn, EXIT_BASE + 60, U, 21600, LONDON))  # NYC -> London: geo fires
    _show("case D", d)
    assert R in _fired(d)
    assert d["priority_score"] == d["total_score"], "confirmed_fraud must veto all demotion for U"
    assert d["priority_adjustment"] is None
    print("    OK: veto in effect, priority_score == total_score, no adjustment")
    print("=== EXIT TEST PASSED ===")
