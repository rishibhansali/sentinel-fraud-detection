from tests.api.conftest import DB_DSN  # noqa: F401


def test_since_id_ascending_any_status_and_next(client, make_case):
    a = make_case("open")
    b = make_case("confirmed_fraud")
    c = make_case("in_review", claimed_by="x")
    d = make_case("false_positive")
    r = client.get("/cases", params={"since_id": a - 1, "limit": 3})
    body = r.json()
    assert set(body) == {"items", "next_since_id"}
    assert [i["id"] for i in body["items"]] == [a, b, c]
    assert body["next_since_id"] == c
    r2 = client.get("/cases", params={"since_id": c})
    ids = [i["id"] for i in r2.json()["items"]]
    assert ids[0] == d and ids == sorted(ids)
    assert all(i > c for i in ids)
    assert r2.json()["next_since_id"] is None  # fewer than limit returned
    assert r2.json()["items"][0]["status"] == "false_positive"
    assert r2.json()["items"][0]["ml_anomaly_score"] is None


def test_since_id_empty(client, make_case):
    a = make_case()
    body = client.get("/cases", params={"since_id": a + 10_000_000_000}).json()
    assert body == {"items": [], "next_since_id": None}


def test_since_id_limit_cap(client):
    assert client.get("/cases", params={"since_id": 0, "limit": 500}).status_code == 200
    assert client.get("/cases", params={"since_id": 0, "limit": 501}).status_code == 422
    assert client.get("/cases", params={"since_id": 0, "limit": 0}).status_code == 422


def test_since_id_with_status_or_cursor_is_422(client):
    assert client.get("/cases", params={"since_id": 1, "status": "open"}).status_code == 422
    assert client.get("/cases", params={"since_id": 1, "cursor": "abc"}).status_code == 422
