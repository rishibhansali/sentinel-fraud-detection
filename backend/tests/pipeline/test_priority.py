"""Phase 5 Task 8: priority demotion (spec section 8). Pure-function tests plus
DB-backed lookup/pipeline tests against REAL Postgres and REAL Redis.
"""
import dataclasses
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import pytest

from app.detection.config import load_rules_config
from app.detection.models import RuleConfig, RuleResult, ScoreResult, Transaction, UserBaseline
from app.detection.scoring import score_transaction
from app.pipeline.loader import get_recent_transactions, get_user_baseline
from app.pipeline.pipeline import make_pipeline_callback, persist_flagged_case
from app.pipeline.priority import (
    CITED_IDS_MAX,
    DECAY,
    FLOOR,
    FORMULA_VERSION,
    PriorityInputs,
    compute_priority,
    demotion_factor,
    lookup_priority_inputs,
)
from app.pipeline.rules_provider import RulesProvider
from app.realtime import publisher
from app.realtime.config import CASES_CHANNEL, redis_url
from app.realtime.subscriber import ThreadedChannelSubscriber
from tests.pipeline.reserved_ranges import (
    PRIORITY_TEST_TXN_BASE,
    PRIORITY_TEST_TXN_MAX,
    PRIORITY_TEST_USER_ID,
)

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)
TS = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)
# this file owns txn ids BASE..BASE+2999 (pipeline txns < +500, prior-case rows +500..)
BASE = PRIORITY_TEST_TXN_BASE
USER = PRIORITY_TEST_USER_ID - 10
OTHER_USER = PRIORITY_TEST_USER_ID - 11
CFG = {
    "velocity": RuleConfig("velocity", 1.0, True, {"window_minutes": 10, "threshold_count": 5}),
    "amount_baseline": RuleConfig("amount_baseline", 1.0, True, {"deviation_multiplier": 3.0}),
    "geo_impossibility": RuleConfig("geo_impossibility", 1.0, True, {"min_distance_km": 50.0, "max_speed_kmh": 900.0}),
}


def _result(fired_subs: dict, unfired_subs: dict = None) -> ScoreResult:
    rrs = [RuleResult(n, True, s, {}) for n, s in fired_subs.items()]
    rrs += [RuleResult(n, False, s, {}) for n, s in (unfired_subs or {}).items()]
    order = list(CFG)
    rrs.sort(key=lambda r: order.index(r.rule_name))
    w = sum(CFG[r.rule_name].weight for r in rrs)
    total = sum(CFG[r.rule_name].weight * r.sub_score for r in rrs) / w
    return ScoreResult(total_score=total, rule_results=rrs)


# ---------------------------------------------------------------- pure tests

def test_constants():
    assert (FORMULA_VERSION, DECAY, FLOOR, CITED_IDS_MAX) == (1, 0.8, 0.5, 10)


def test_factor_curve():
    got = [demotion_factor(n) for n in range(1, 7)]
    assert got == pytest.approx([0.8, 0.64, 0.512, 0.5, 0.5, 0.5])
    assert got[3] == 0.5 and got[5] == 0.5  # floor is exact


def test_no_history_means_no_adjustment_and_exact_parity():
    res = _result({"geo_impossibility": 3.3})
    score, adj = compute_priority(res, CFG, PriorityInputs())
    assert adj is None and score == res.total_score


def test_parity_with_score_transaction_randomized():
    """The local combiner duplicates scoring.py's; with no adjustment the two
    must agree EXACTLY over many seeded random inputs (incl. disabled rules,
    zero weights, all-disabled)."""
    rng = random.Random(20260921)
    for _ in range(500):
        cfg = {
            n: RuleConfig(
                n, rng.choice([0.0, 0.1, 0.5, 1.0, 2.5, rng.random() * 5]), rng.random() < 0.7, c.params
            )
            for n, c in CFG.items()
        }
        ts = TS + timedelta(minutes=rng.randint(0, 5000))

        def loc():
            return rng.uniform(-60, 60), rng.uniform(-170, 170)

        lat, lon = loc()
        txn = Transaction(1, 1, ts, rng.uniform(1, 500), lat, lon)
        recent = []
        for i in range(rng.randint(0, 8)):
            la, lo = loc()
            recent.append(Transaction(100 + i, 1, ts - timedelta(minutes=rng.randint(0, 30) + i), 10.0, la, lo))
        recent.sort(key=lambda t: t.ts, reverse=True)
        baseline = UserBaseline(rng.randint(0, 50), rng.uniform(0, 300), 0.0, 0, None)
        res = score_transaction(txn, recent, baseline, cfg)
        score, adj = compute_priority(res, cfg, PriorityInputs())
        assert adj is None
        assert score == res.total_score  # exact, not approx


def test_only_fired_rule_with_prior_fps_is_demoted():
    res = _result({"geo_impossibility": 3.0, "velocity": 1.0}, {"amount_baseline": 0.2})
    inputs = PriorityInputs(false_positives={"geo_impossibility": (2, [5, 9])})
    score, adj = compute_priority(res, CFG, inputs)
    assert score == pytest.approx((1.0 + 0.2 + 3.0 * 0.64) / 3)
    assert score < res.total_score
    assert adj == {
        "formula_version": 1, "decay": 0.8, "floor": 0.5, "veto": False,
        "per_rule": [{
            "rule_name": "geo_impossibility", "prior_false_positive_count": 2,
            "prior_false_positive_case_ids": [5, 9], "factor": pytest.approx(0.64),
        }],
    }


def test_unfired_rule_with_fp_history_is_not_adjusted():
    res = _result({"geo_impossibility": 3.0}, {"velocity": 0.4})
    inputs = PriorityInputs(false_positives={"velocity": (3, [1, 2, 3])})
    score, adj = compute_priority(res, CFG, inputs)
    assert adj is None and score == res.total_score


def test_veto_cites_suppressed_demotion_without_changing_priority():
    res = _result({"geo_impossibility": 3.0})
    inputs = PriorityInputs(veto=True, false_positives={"geo_impossibility": (9, [1])})
    score, adj = compute_priority(res, CFG, inputs)
    assert score == res.total_score
    assert adj == {
        "formula_version": 1, "decay": 0.8, "floor": 0.5, "veto": True,
        "per_rule": [{
            "rule_name": "geo_impossibility", "prior_false_positive_count": 9,
            "prior_false_positive_case_ids": [1], "factor": 1.0,
        }],
    }


def test_disabled_rule_not_demoted_or_combined():
    cfg = dict(CFG)
    cfg["geo_impossibility"] = dataclasses.replace(CFG["geo_impossibility"], enabled=False)
    res = _result({"velocity": 1.0, "geo_impossibility": 3.0})  # even if a result claims fired
    inputs = PriorityInputs(false_positives={"geo_impossibility": (1, [1]), "velocity": (1, [2])})
    score, adj = compute_priority(res, cfg, inputs)
    assert [p["rule_name"] for p in adj["per_rule"]] == ["velocity"]
    # combined over enabled rules only: velocity (demoted); geo (disabled) is excluded
    assert score == pytest.approx(1.0 * 0.8)


def test_all_rules_disabled_guard():
    cfg = {n: dataclasses.replace(c, enabled=False) for n, c in CFG.items()}
    res = ScoreResult(0.0, [RuleResult(n, False, 0.0, {}) for n in cfg])
    assert compute_priority(res, cfg, PriorityInputs()) == (0.0, None)


def test_citation_caps_ids_at_ten_but_keeps_full_count():
    res = _result({"velocity": 1.0})
    ids = list(range(100, 88, -1))  # 12 ids, newest first (lookup caps at 10; be defensive)
    _, adj = compute_priority(res, CFG, PriorityInputs(false_positives={"velocity": (12, ids)}))
    p = adj["per_rule"][0]
    assert p["prior_false_positive_count"] == 12
    assert len(p["prior_false_positive_case_ids"]) == CITED_IDS_MAX
    assert p["factor"] == FLOOR


def test_bounded_demotion_property():
    rng = random.Random(7)
    names = list(CFG)
    for _ in range(2000):
        fired = {n: rng.uniform(0.01, 10) for n in names if rng.random() < 0.6}
        if not fired:
            fired = {names[0]: 1.0}
        unfired = {n: rng.uniform(0, 1) for n in names if n not in fired}
        res = _result(fired, unfired)
        fps = {n: (rng.randint(1, 30), [1]) for n in names if rng.random() < 0.7}
        score, _ = compute_priority(res, CFG, PriorityInputs(veto=rng.random() < 0.2, false_positives=fps))
        assert score >= FLOOR * res.total_score - 1e-12
        assert score <= res.total_score + 1e-12


# ------------------------------------------------------------------ DB tests

def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _cleanup(c):
    with c.cursor() as cur:
        cur.execute(
            "DELETE FROM case_feedback WHERE case_id IN "
            "(SELECT id FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s)",
            (BASE, BASE + 2999),
        )
        cur.execute("DELETE FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s", (BASE, BASE + 2999))
        cur.execute("DELETE FROM transactions WHERE id BETWEEN %s AND %s", (BASE, BASE + 2999))
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


@pytest.fixture(autouse=True)
def _reset_pub():
    publisher.reset_publish_failure_count()
    yield


_counter = [500]


def _prior_case(conn, user, status, fired=(), unfired=()):
    """Insert a decided prior case directly (lookup-level tests only)."""
    _counter[0] += 1
    rrs = [{"rule_name": n, "fired": True, "sub_score": 2.0, "details": {}} for n in fired]
    rrs += [{"rule_name": n, "fired": False, "sub_score": 0.0, "details": {}} for n in unfired]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO flagged_cases (transaction_id, transaction_ts, user_id, total_score, rule_results, "
            "priority_score, status) VALUES (%s, %s, %s, 0.5, %s, 0.5, %s) RETURNING id",
            (BASE + _counter[0], TS, user, json.dumps(rrs), status),
        )
        cid = cur.fetchone()[0]
    conn.commit()
    return cid


def _insert_txn(conn, id_, ts, lat, lon, user=USER):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (id, user_id, card_id, ts, amount, lat, lon,
                v1, v2, v3, v4, v5, v6, v7, v8, v9, v10, v11, v12, v13, v14, v15, v16, v17, v18, v19, v20,
                v21, v22, v23, v24, v25, v26, v27, v28, class, split)
            VALUES (%s, %s, 1, %s, 10.0, %s, %s,
                0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0, 0, 'test')
            """,
            (id_, user, ts, lat, lon),
        )
    conn.commit()
    return Transaction(id=id_, user_id=user, ts=ts, amount=10.0, lat=lat, lon=lon)


def _seed_geo(conn, base=BASE, user=USER):
    _insert_txn(conn, base, TS, *NYC, user=user)
    return _insert_txn(conn, base + 1, TS + timedelta(seconds=270), *LONDON, user=user)


def _seed_geo_and_velocity(conn, base=BASE, user=USER):
    for i in range(4):
        _insert_txn(conn, base + i, TS + timedelta(seconds=30 * i), *NYC, user=user)
    return _insert_txn(conn, base + 10, TS + timedelta(seconds=270), *LONDON, user=user)


def _case(txn_id):
    c = psycopg2.connect(DB_DSN)
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM flagged_cases WHERE transaction_id = %s", (txn_id,))
            return cur.fetchone()
    finally:
        c.close()


def _run(conn, txn, cfg=None):
    cfg = cfg or load_rules_config(DB_DSN)
    make_pipeline_callback(conn, RulesProvider.static(cfg, None))(txn)
    return _case(txn.id)


@pytest.mark.parametrize("n,expected", [(1, 0.8), (2, 0.64), (3, 0.512), (4, 0.5), (5, 0.5), (6, 0.5)])
def test_pipeline_factor_curve_n_1_to_6(conn, n, expected):
    for _ in range(n):
        _prior_case(conn, USER, "false_positive", fired=["geo_impossibility"])
    row = _run(conn, _seed_geo(conn))
    per = row["priority_adjustment"]["per_rule"][0]
    assert per["prior_false_positive_count"] == n
    assert per["factor"] == pytest.approx(expected)
    subs = {r["rule_name"]: r["sub_score"] for r in row["rule_results"]}
    subs["geo_impossibility"] *= expected  # only the fired, previously-false-positive rule is scaled
    assert row["priority_score"] == pytest.approx(sum(subs.values()) / 3)
    assert row["priority_score"] < row["total_score"]
    assert row["priority_score"] >= FLOOR * row["total_score"]


def test_no_prior_feedback_no_adjustment(conn):
    row = _run(conn, _seed_geo(conn))
    assert row["priority_score"] == row["total_score"]
    assert row["priority_adjustment"] is None


def test_case_inserted_published_and_total_score_untouched(conn):
    got = []
    sub = ThreadedChannelSubscriber(redis_url(), CASES_CHANNEL, got.append)
    sub.start()
    try:
        assert sub.wait_ready(5)
        prior = _prior_case(conn, USER, "false_positive", fired=["geo_impossibility"])
        txn = _seed_geo(conn)
        cfg = load_rules_config(DB_DSN)
        expected_total = score_transaction(
            txn, get_recent_transactions(conn, USER, txn.ts, txn.id, 100), get_user_baseline(conn, USER), cfg
        ).total_score
        row = _run(conn, txn, cfg)
        assert row is not None  # never suppressed
        assert row["total_score"] == expected_total
        assert row["priority_score"] < row["total_score"]
        assert row["priority_adjustment"]["per_rule"][0]["prior_false_positive_case_ids"] == [prior]

        def mine():
            out = []
            for m in got:
                m = json.loads(m) if isinstance(m, (str, bytes)) else m
                if m.get("type") == "case.created" and m["case"]["id"] == row["id"]:
                    out.append(m)
            return out

        assert _wait(lambda: len(mine()) == 1)
        case = mine()[0]["case"]
        assert case["priority_score"] == row["priority_score"]
        assert case["total_score"] == row["total_score"]
    finally:
        sub.stop()


def test_any_confirmed_fraud_vetoes_all_demotion(conn):
    prior_ids = [
        _prior_case(conn, USER, "false_positive", fired=["geo_impossibility"])
        for _ in range(3)
    ]
    _prior_case(conn, USER, "confirmed_fraud", fired=["amount_baseline"])  # a DIFFERENT rule still vetoes
    row = _run(conn, _seed_geo(conn))
    assert row is not None
    assert row["priority_score"] == row["total_score"]
    adjustment = row["priority_adjustment"]
    assert adjustment["veto"] is True
    assert adjustment["per_rule"] == [{
        "rule_name": "geo_impossibility", "prior_false_positive_count": 3,
        "prior_false_positive_case_ids": prior_ids, "factor": 1.0,
    }]


def test_other_users_cases_do_not_count(conn):
    _prior_case(conn, OTHER_USER, "false_positive", fired=["geo_impossibility"])
    _prior_case(conn, OTHER_USER, "confirmed_fraud", fired=["geo_impossibility"])
    row = _run(conn, _seed_geo(conn))
    assert row["priority_score"] == row["total_score"] and row["priority_adjustment"] is None


def test_only_cases_where_this_rule_fired_count(conn):
    _prior_case(conn, USER, "false_positive", fired=["velocity"], unfired=["geo_impossibility"])
    _prior_case(conn, USER, "false_positive", fired=["amount_baseline"])
    row = _run(conn, _seed_geo(conn))
    assert row["priority_score"] == row["total_score"] and row["priority_adjustment"] is None


def test_open_and_in_review_cases_do_not_count(conn):
    _prior_case(conn, USER, "open", fired=["geo_impossibility"])
    cid = _prior_case(conn, USER, "open", fired=["geo_impossibility"])
    with conn.cursor() as cur:
        cur.execute("UPDATE flagged_cases SET status='in_review', claimed_by='a', claimed_at=now() WHERE id=%s", (cid,))
    conn.commit()
    row = _run(conn, _seed_geo(conn))
    assert row["priority_adjustment"] is None


def test_correction_fp_to_confirmed_removes_influence_and_vetoes(conn):
    cid = _prior_case(conn, USER, "false_positive", fired=["geo_impossibility"])
    assert lookup_priority_inputs(conn, USER, ["geo_impossibility"]).false_positives["geo_impossibility"][0] == 1
    with conn.cursor() as cur:  # current status flips (as recompute_status does after a later decision)
        cur.execute("UPDATE flagged_cases SET status='confirmed_fraud' WHERE id=%s", (cid,))
    conn.commit()
    inputs = lookup_priority_inputs(conn, USER, ["geo_impossibility"])
    assert inputs.false_positives == {} and inputs.veto is True
    row = _run(conn, _seed_geo(conn))
    assert row["priority_score"] == row["total_score"] and row["priority_adjustment"] is None


def test_multi_rule_case_demotes_only_rules_with_prior_fps(conn):
    _prior_case(conn, USER, "false_positive", fired=["geo_impossibility"], unfired=["velocity"])
    row = _run(conn, _seed_geo_and_velocity(conn))
    fired = {r["rule_name"] for r in row["rule_results"] if r["fired"]}
    assert {"velocity", "geo_impossibility"} <= fired
    adj = row["priority_adjustment"]
    assert [p["rule_name"] for p in adj["per_rule"]] == ["geo_impossibility"]
    subs = {r["rule_name"]: r["sub_score"] for r in row["rule_results"]}
    expected = (subs["velocity"] + subs["amount_baseline"] + subs["geo_impossibility"] * 0.8) / 3
    assert row["priority_score"] == pytest.approx(expected)


def test_disabled_rule_is_not_demoted_or_combined(conn):
    """Pipeline level: with geo disabled, geo cannot fire, so the fp history
    for geo neither flags nor adjusts anything; a still-enabled fired rule
    (velocity, no fp history) is combined without geo in the weights."""
    _prior_case(conn, USER, "false_positive", fired=["geo_impossibility"])
    cfg = dict(load_rules_config(DB_DSN))
    cfg["geo_impossibility"] = dataclasses.replace(cfg["geo_impossibility"], enabled=False)
    row = _run(conn, _seed_geo_and_velocity(conn), cfg)
    assert row is not None
    assert row["priority_adjustment"] is None
    assert row["priority_score"] == row["total_score"]
    assert row["total_score"] == pytest.approx(
        next(r["sub_score"] for r in row["rule_results"] if r["rule_name"] == "velocity") / 2
    )


def test_citation_lists_at_most_ten_ids_with_full_count(conn):
    ids = [_prior_case(conn, USER, "false_positive", fired=["geo_impossibility"]) for _ in range(12)]
    row = _run(conn, _seed_geo(conn))
    p = row["priority_adjustment"]["per_rule"][0]
    assert p["prior_false_positive_count"] == 12
    assert p["prior_false_positive_case_ids"] == sorted(ids[-10:])  # most recent 10
    assert p["factor"] == FLOOR
    assert row["priority_adjustment"]["formula_version"] == 1


def test_persist_defaults_unchanged_for_existing_callers(conn):
    txn = _seed_geo(conn)
    res = score_transaction(txn, [], UserBaseline(0, 0.0, 0.0, 0, None), load_rules_config(DB_DSN))
    res = dataclasses.replace(res, total_score=0.42)
    assert persist_flagged_case(conn, txn, res) is not None
    conn.commit()
    row = _case(txn.id)
    assert row["priority_score"] == 0.42 and row["priority_adjustment"] is None
