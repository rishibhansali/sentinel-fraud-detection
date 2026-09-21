"""Rules admin API against REAL Postgres and REAL Redis."""
import threading
import time

import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline.loader import DEFAULT_ROW_CAP
from app.realtime.config import RULES_CHANNEL, redis_url
from app.realtime.subscriber import ThreadedChannelSubscriber
from tests.rules.conftest import (
    DB_DSN, direct_change, fetch_rules, history_rows, max_history_id,
)

BODY = {"changed_by": "tester"}


def patch(client, rule, **body):
    return client.patch(f"/rules/{rule}", json={**BODY, **body})


def assert_unchanged(guard):
    assert fetch_rules() == guard.rules
    assert max_history_id() == guard.max_id


# ---------------- validation matrix ----------------

INVALID = [
    ("velocity", {"weight": -0.1}),
    ("velocity", {"weight": float("nan")}),
    ("velocity", {"weight": float("inf")}),
    ("velocity", {"weight": True}),
    ("velocity", {"weight": "1.0"}),
    ("velocity", {"enabled": "yes"}),
    ("velocity", {"enabled": 1}),
    ("velocity", {"params": {"window_minutes": 0}}),
    ("velocity", {"params": {"window_minutes": -5}}),
    ("velocity", {"params": {"window_minutes": True}}),
    ("velocity", {"params": {"window_minutes": 2.5}}),
    ("velocity", {"params": {"window_minutes": 10.0}}),  # float for int rejected
    ("velocity", {"params": {"threshold_count": 1}}),
    ("velocity", {"params": {"threshold_count": DEFAULT_ROW_CAP + 1}}),
    ("velocity", {"params": {"threshold_count": True}}),
    ("velocity", {"params": {"threshold_count": 5.5}}),
    ("velocity", {"params": {"threshold_count": "5"}}),
    ("velocity", {"params": {"bogus": 1}}),
    ("velocity", {"params": {"deviation_multiplier": 3.0}}),
    ("velocity", {"params": {}}),
    ("velocity", {"params": []}),
    ("amount_baseline", {"params": {"deviation_multiplier": 1.0}}),
    ("amount_baseline", {"params": {"deviation_multiplier": 0.5}}),
    ("amount_baseline", {"params": {"deviation_multiplier": float("nan")}}),
    ("amount_baseline", {"params": {"deviation_multiplier": float("inf")}}),
    ("amount_baseline", {"params": {"deviation_multiplier": True}}),
    ("amount_baseline", {"params": {"deviation_multiplier": "3"}}),
    ("amount_baseline", {"params": {"window_minutes": 3}}),
    ("geo_impossibility", {"params": {"min_distance_km": -1}}),
    ("geo_impossibility", {"params": {"min_distance_km": float("inf")}}),
    ("geo_impossibility", {"params": {"min_distance_km": False}}),
    ("geo_impossibility", {"params": {"max_speed_kmh": 0}}),
    ("geo_impossibility", {"params": {"max_speed_kmh": -10}}),
    ("geo_impossibility", {"params": {"max_speed_kmh": float("nan")}}),
    ("geo_impossibility", {"params": {"nope": 1}}),
]


def _raw_patch(client, rule, obj):
    # NaN/inf are not valid strict JSON, so send them the way a lenient client would.
    import json
    return client.patch(
        f"/rules/{rule}", content=json.dumps(obj), headers={"content-type": "application/json"}
    )


@pytest.mark.parametrize("rule,fields", INVALID, ids=[f"{r}-{i}" for i, (r, _) in enumerate(INVALID)])
def test_invalid_values_422_and_nothing_written(client, rules_guard, rule, fields):
    r = _raw_patch(client, rule, {**BODY, **fields})
    assert r.status_code == 422, r.text
    assert_unchanged(rules_guard)


@pytest.mark.parametrize("rule,fields", [
    ("velocity", {"weight": 0}),
    ("velocity", {"weight": 2}),
    ("velocity", {"params": {"window_minutes": 1}}),
    ("velocity", {"params": {"threshold_count": 2}}),
    ("velocity", {"params": {"threshold_count": DEFAULT_ROW_CAP}}),
    ("amount_baseline", {"params": {"deviation_multiplier": 1.0001}}),
    ("amount_baseline", {"params": {"deviation_multiplier": 4}}),
    ("geo_impossibility", {"params": {"min_distance_km": 0}}),
    ("geo_impossibility", {"params": {"max_speed_kmh": 0.001}}),
])
def test_boundary_values_accepted(client, rule, fields):
    r = patch(client, rule, **fields)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True


def test_empty_body_and_missing_or_blank_changed_by(client, rules_guard):
    assert client.patch("/rules/velocity", json={}).status_code == 422
    assert client.patch("/rules/velocity", json={"changed_by": "x"}).status_code == 422  # no field
    assert client.patch("/rules/velocity", json={"weight": 2.0}).status_code == 422
    for cb in ("", "   ", None, 5):
        assert client.patch("/rules/velocity", json={"changed_by": cb, "weight": 2.0}).status_code == 422
    assert client.patch("/rules/velocity", json={"changed_by": "x", "weight": 2.0, "junk": 1}).status_code == 422
    assert client.patch(
        "/rules/velocity", content="[1]", headers={"content-type": "application/json"}
    ).status_code == 422
    assert_unchanged(rules_guard)


def test_unknown_rule_404(client, rules_guard):
    assert patch(client, "no_such_rule", weight=2.0).status_code == 404
    assert_unchanged(rules_guard)


def test_cannot_disable_last_enabled_rule(client, rules_guard):
    assert patch(client, "velocity", enabled=False).status_code == 200
    assert patch(client, "amount_baseline", enabled=False).status_code == 200
    before = max_history_id()
    r = patch(client, "geo_impossibility", enabled=False)
    assert r.status_code == 422
    assert max_history_id() == before
    assert [x["enabled"] for x in fetch_rules() if x["rule_name"] == "geo_impossibility"] == [True]


def test_disable_then_reenable(client):
    assert patch(client, "velocity", enabled=False).status_code == 200
    assert patch(client, "velocity", enabled=True).status_code == 200


# ---------------- write path ----------------

def test_patch_writes_rules_and_history(client, rules_guard):
    orig = {r["rule_name"]: r for r in rules_guard.rules}["velocity"]
    r = patch(client, "velocity", weight=2.5, changed_by="alice")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True and body["affects_flagging"] is False
    assert "weight" in body["weight_note"]
    hist = history_rows(rules_guard.max_id)
    assert len(hist) == 1
    h = hist[0]
    assert body["version"] == h["id"] == max_history_id()
    assert h["changed_by"] == "alice" and h["rule_name"] == "velocity"
    assert h["before"] == {"weight": orig["weight"], "enabled": orig["enabled"], "params": orig["params"]}
    assert h["after"] == {"weight": 2.5, "enabled": orig["enabled"], "params": orig["params"]}
    row = {x["rule_name"]: x for x in fetch_rules()}["velocity"]
    assert row["weight"] == 2.5
    assert row["updated_at"] > orig["updated_at"]
    g = client.get("/rules").json()
    assert g["version"] == h["id"]
    v = {x["rule_name"]: x for x in g["rules"]}["velocity"]
    assert v["weight"] == 2.5 and v["params"] == orig["params"]
    assert body["rule"]["weight"] == 2.5


def test_changed_by_is_stripped(client, rules_guard):
    patch(client, "velocity", weight=3.0, changed_by="  bob  ")
    assert history_rows(rules_guard.max_id)[0]["changed_by"] == "bob"


def test_params_shallow_merge(client, rules_guard):
    orig = {r["rule_name"]: r for r in rules_guard.rules}["velocity"]["params"]
    r = patch(client, "velocity", params={"threshold_count": 7})
    assert r.status_code == 200
    assert r.json()["rule"]["params"] == {**orig, "threshold_count": 7}
    h = history_rows(rules_guard.max_id)[0]
    assert h["before"]["params"] == orig and h["after"]["params"] == {**orig, "threshold_count": 7}


def test_affects_flagging_flags(client):
    a = patch(client, "velocity", weight=1.7).json()
    assert (a["changed"], a["affects_flagging"]) == (True, False)
    b = patch(client, "velocity", params={"threshold_count": 6}).json()
    assert (b["changed"], b["affects_flagging"]) == (True, True)
    c = patch(client, "velocity", enabled=False).json()
    assert (c["changed"], c["affects_flagging"]) == (True, True)
    # weight changes, params supplied but equal: only weight changed value
    d = patch(client, "velocity", weight=9.0, params={"threshold_count": 6}).json()
    assert (d["changed"], d["affects_flagging"]) == (True, False)
    assert d["version"] > c["version"] > b["version"] > a["version"]


def test_noop_writes_nothing(client, rules_guard):
    cur = {r["rule_name"]: r for r in rules_guard.rules}["velocity"]
    r = patch(client, "velocity", weight=cur["weight"], enabled=cur["enabled"], params=dict(cur["params"]))
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is False and body["affects_flagging"] is False
    assert body["version"] == rules_guard.max_id
    assert_unchanged(rules_guard)


def test_noop_publishes_nothing(client):
    got = []
    sub = ThreadedChannelSubscriber(redis_url(), RULES_CHANNEL, got.append)
    sub.start()
    try:
        assert sub.wait_ready(5)
        cur = client.get("/rules").json()["rules"][0]
        r = patch(client, cur["rule_name"], enabled=cur["enabled"])
        assert r.json()["changed"] is False
        time.sleep(0.5)
        assert got == []
    finally:
        sub.stop()


def test_history_ordering_filtering_and_limit(client, rules_guard):
    v1 = patch(client, "velocity", weight=2.0).json()["version"]
    a1 = patch(client, "amount_baseline", weight=2.0).json()["version"]
    v2 = patch(client, "velocity", weight=3.0).json()["version"]
    h = client.get("/rules/history").json()["history"]
    ids = [x["id"] for x in h]
    assert ids == sorted(ids, reverse=True)
    assert ids[:3] == [v2, a1, v1]
    hv = client.get("/rules/history", params={"rule_name": "velocity", "limit": 2}).json()["history"]
    assert [x["id"] for x in hv] == [v2, v1]
    assert all(x["rule_name"] == "velocity" for x in hv)
    assert set(hv[0]) == {"id", "rule_name", "before", "after", "changed_by", "changed_at"}
    assert client.get("/rules/history", params={"limit": 0}).status_code == 422
    assert client.get("/rules/history", params={"limit": 201}).status_code == 422
    assert len(client.get("/rules/history", params={"limit": 200}).json()["history"]) <= 200
    assert len(client.get("/rules/history").json()["history"]) <= 50
    assert client.get("/rules/history", params={"rule_name": "zzz"}).json()["history"] == []


def test_get_rules_shape(client, rules_guard):
    g = client.get("/rules").json()
    assert g["version"] == rules_guard.max_id
    assert {r["rule_name"] for r in g["rules"]} == {"velocity", "amount_baseline", "geo_impossibility"}
    assert set(g["rules"][0]) == {"rule_name", "weight", "enabled", "params", "updated_at"}


# ---------------- publish after commit ----------------

def test_publish_after_commit_sees_new_state(client):
    seen = []

    def on_message(msg):
        c = psycopg2.connect(DB_DSN)  # SEPARATE connection
        try:
            with c.cursor() as cur:
                cur.execute("SELECT weight FROM rules_config WHERE rule_name='velocity'")
                w = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM rules_config_history WHERE id=%s", (msg["version"],))
                n = cur.fetchone()[0]
            seen.append((msg, w, n))
        finally:
            c.close()

    sub = ThreadedChannelSubscriber(redis_url(), RULES_CHANNEL, on_message)
    sub.start()
    try:
        assert sub.wait_ready(5)
        version = patch(client, "velocity", weight=4.25).json()["version"]
        deadline = time.time() + 5
        while time.time() < deadline and not any(m["version"] == version for m, _, _ in seen):
            time.sleep(0.02)
        mine = [s for s in seen if s[0]["version"] == version]
        assert mine, seen
        msg, w, n = mine[0]
        assert msg == {"version": version} and w == 4.25 and n == 1
    finally:
        sub.stop()


def test_failures_publish_nothing(client):
    got = []
    sub = ThreadedChannelSubscriber(redis_url(), RULES_CHANNEL, got.append)
    sub.start()
    try:
        assert sub.wait_ready(5)
        assert patch(client, "velocity", weight=-1).status_code == 422
        assert patch(client, "nope", weight=1).status_code == 404
        assert client.patch("/rules/velocity", json={"weight": 1}).status_code == 422
        time.sleep(0.5)
        assert got == []
    finally:
        sub.stop()


# ---------------- concurrency ----------------

def test_concurrent_disables_exactly_one_wins(client):
    direct_change("geo_impossibility", enabled=False)
    assert sorted(r["rule_name"] for r in fetch_rules() if r["enabled"]) == ["amount_baseline", "velocity"]

    results = {}
    barrier = threading.Barrier(2)

    def run(rule):
        with TestClient(create_app()) as c:
            barrier.wait()
            results[rule] = c.patch(f"/rules/{rule}", json={"changed_by": "t", "enabled": False}).status_code

    ts = [threading.Thread(target=run, args=(r,)) for r in ("velocity", "amount_baseline")]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert sorted(results.values()) == [200, 422], results
    assert any(r["enabled"] for r in fetch_rules())
