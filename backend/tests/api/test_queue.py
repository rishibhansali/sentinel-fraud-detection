from tests.api.conftest import TOP


def walk(client, params, page_size, want):
    """Follow next_cursor until `want` items are collected (our rows sit on top)."""
    items, cursor, pages = [], None, 0
    while len(items) < want:
        q = dict(params, limit=page_size)
        if cursor:
            q["cursor"] = cursor
        r = client.get("/cases", params=q)
        assert r.status_code == 200
        body = r.json()
        assert len(body["items"]) <= page_size
        items += body["items"]
        pages += 1
        cursor = body["next_cursor"]
        assert cursor is not None or len(items) >= want
        assert pages < 100
    return items[:want], pages


def test_keyset_pagination_with_ties(client, make_case):
    ids = []
    # 3 distinct scores, many ties within each
    for score in (TOP, TOP + 1, TOP - 1):
        for _ in range(9):
            ids.append((score, make_case(priority=score)))
    expected = [i for _, i in sorted(ids, key=lambda t: (-t[0], -t[1]))]
    for page_size in (1, 4, 5, 27):
        items, _ = walk(client, {}, page_size, len(expected))
        got = [i["id"] for i in items]
        assert got == expected  # no dup, no gap, exact order
        assert len(set(got)) == len(got)


def test_default_filter_is_pending(client, make_case):
    o = make_case("open")
    ir = make_case("in_review", claimed_by="a")
    cf = make_case("confirmed_fraud")
    fp = make_case("false_positive")
    items, _ = walk(client, {}, 50, 2)
    assert {i["id"] for i in items} == {o, ir}
    assert {i["id"] for i in items}.isdisjoint({cf, fp})


def test_explicit_status_filters(client, make_case):
    o = make_case("open", priority=TOP)
    ir = make_case("in_review", claimed_by="a", priority=TOP)
    cf = make_case("confirmed_fraud", priority=TOP)
    fp = make_case("false_positive", priority=TOP)
    r = client.get("/cases", params={"status": "confirmed_fraud", "limit": 1})
    assert [i["id"] for i in r.json()["items"]] == [cf]
    items, _ = walk(client, {"status": "open,false_positive"}, 10, 2)
    assert {i["id"] for i in items} == {o, fp}
    items, _ = walk(client, {"status": "in_review"}, 10, 1)
    assert items[0]["id"] == ir
    assert client.get("/cases", params={"status": "bogus"}).status_code == 422


def test_limit_bounds(client, make_case):
    make_case()
    assert client.get("/cases", params={"limit": 0}).status_code == 422
    assert client.get("/cases", params={"limit": 201}).status_code == 422
    assert client.get("/cases", params={"limit": 200}).status_code == 200
    assert client.get("/cases", params={"limit": 1}).status_code == 200
    body = client.get("/cases").json()
    assert set(body) == {"items", "next_cursor"}
    assert len(body["items"]) <= 50


def test_bad_cursor_is_422(client):
    assert client.get("/cases", params={"cursor": "not-a-cursor"}).status_code == 422


def test_item_shape_and_null_ml_score(client, make_case):
    cid = make_case(fired=("velocity",), unfired=("geo",))
    item = client.get("/cases", params={"limit": 200}).json()["items"]
    mine = next(i for i in item if i["id"] == cid)
    assert mine["ml_anomaly_score"] is None
    assert mine["fired_rules"] == ["velocity"]
    for k in ("claimed_at", "transaction_id", "transaction_ts", "rules_config_version",
              "status", "claimed_by", "priority_score", "total_score", "flagged_at", "user_id"):
        assert k in mine
    assert isinstance(mine["flagged_at"], str) and "T" in mine["flagged_at"]
    assert isinstance(mine["transaction_ts"], str)
