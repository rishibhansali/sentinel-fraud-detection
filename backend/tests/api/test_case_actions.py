import time


def post(client, cid, action, **body):
    return client.post(f"/cases/{cid}/{action}", json=body)


def test_detail_and_404(client, make_case):
    cid = make_case(fired=("velocity",))
    d = client.get(f"/cases/{cid}").json()
    assert d["id"] == cid
    assert d["ml_anomaly_score"] is None
    assert d["feedback"] == [] and d["current_decision"] is None
    for k in ("rule_results", "priority_score", "priority_adjustment", "rules_config_version",
              "claimed_by", "claimed_at", "status", "transaction_id", "transaction_ts",
              "user_id", "total_score", "flagged_at"):
        assert k in d
    assert d["rule_results"][0]["rule_name"] == "velocity"
    assert client.get("/cases/999999999999").status_code == 404


def test_claim_and_conflict(client, make_case):
    cid = make_case()
    r = post(client, cid, "claim", analyst="alice")
    assert r.status_code == 200
    assert r.json()["status"] == "in_review" and r.json()["claimed_by"] == "alice"
    r = post(client, cid, "claim", analyst="bob")
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["claimed_by"] == "alice" and d["current_status"] == "in_review" and d["reason"]


def test_release(client, make_case):
    cid = make_case()
    post(client, cid, "claim", analyst="alice")
    assert post(client, cid, "release", analyst="bob").status_code == 409
    r = post(client, cid, "release", analyst="alice")
    assert r.status_code == 200
    assert r.json()["status"] == "open" and r.json()["claimed_by"] is None


def test_feedback_flow_and_correction(client, make_case):
    cid = make_case()
    post(client, cid, "claim", analyst="alice")
    r = post(client, cid, "feedback", analyst="bob", decision="confirmed_fraud")
    assert r.status_code == 409
    r = post(client, cid, "feedback", analyst="alice", decision="false_positive", note="looked fine")
    assert r.status_code == 200 and r.json()["status"] == "false_positive"
    r = post(client, cid, "feedback", analyst="bob", decision="confirmed_fraud")
    assert r.status_code == 200 and r.json()["status"] == "confirmed_fraud"
    d = client.get(f"/cases/{cid}").json()
    assert [f["analyst"] for f in d["feedback"]] == ["alice", "bob"]
    assert d["feedback"][0]["note"] == "looked fine"
    assert set(d["feedback"][0]) == {"id", "decision", "analyst", "note", "decided_at"}
    assert d["current_decision"]["analyst"] == "bob"
    assert d["current_decision"]["decision"] == "confirmed_fraud"
    assert d["status"] == "confirmed_fraud"


def test_feedback_on_open_case_allowed(client, make_case):
    cid = make_case()
    assert post(client, cid, "feedback", analyst="a", decision="false_positive").status_code == 200


def test_422_and_404(client, make_case):
    cid = make_case()
    assert post(client, cid, "feedback", analyst="a", decision="maybe").status_code == 422
    assert post(client, cid, "feedback", analyst="a").status_code == 422
    assert post(client, cid, "feedback", decision="false_positive").status_code == 422
    assert post(client, cid, "feedback", analyst="   ", decision="false_positive").status_code == 422
    assert post(client, cid, "claim", analyst="").status_code == 422
    assert post(client, cid, "claim").status_code == 422
    assert post(client, cid, "release", analyst=" ").status_code == 422
    big = 999999999999
    assert post(client, big, "claim", analyst="a").status_code == 404
    assert post(client, big, "release", analyst="a").status_code == 404
    assert post(client, big, "feedback", analyst="a", decision="false_positive").status_code == 404
    assert client.get(f"/cases/{cid}").json()["status"] == "open"  # 422s changed nothing


def test_pool_connections_are_returned(client, make_case):
    """Many failing + succeeding requests must not exhaust the pool (max 10)."""
    cid = make_case()
    post(client, cid, "claim", analyst="a")
    for _ in range(30):
        assert post(client, cid, "claim", analyst="b").status_code == 409
        assert client.get("/cases/999999999999").status_code == 404
        assert client.get("/cases", params={"limit": 1}).status_code == 200


def test_works_with_lifespan_and_pool_reopens_after_shutdown(make_case):
    from fastapi.testclient import TestClient

    from app.main import app

    cid = make_case()
    with TestClient(app) as c:
        assert c.get(f"/cases/{cid}").status_code == 200
    # lifespan shutdown closed the pool; next use lazily recreates it
    assert TestClient(app).get(f"/cases/{cid}").status_code == 200
