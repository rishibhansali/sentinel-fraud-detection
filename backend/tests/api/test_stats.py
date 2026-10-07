def by_name(client):
    r = client.get("/stats/rules")
    assert r.status_code == 200
    return {x["rule_name"]: x for x in r.json()}


def decide(client, cid, decision, analyst="a"):
    assert client.post(f"/cases/{cid}/feedback",
                       json={"analyst": analyst, "decision": decision}).status_code == 200


def test_stats_counts_and_precision(client, make_case):
    A, B, C = "zz_api_rule_a", "zz_api_rule_b", "zz_api_rule_c"
    c1 = make_case(fired=(A, B))
    c2 = make_case(fired=(A,), unfired=(B,))
    c3 = make_case(fired=(A,))
    c4 = make_case(fired=(C,))            # left open: must not count
    c5 = make_case(fired=(B,), unfired=(A,))  # decided fp then corrected to confirmed
    decide(client, c1, "confirmed_fraud")
    decide(client, c2, "false_positive")
    decide(client, c3, "confirmed_fraud")
    decide(client, c5, "false_positive")
    decide(client, c5, "confirmed_fraud", analyst="b")  # current status wins
    s = by_name(client)
    assert (s[A]["confirmed_fraud"], s[A]["false_positive"]) == (2, 1)
    assert s[A]["precision"] == 2 / 3
    assert (s[B]["confirmed_fraud"], s[B]["false_positive"]) == (2, 0)
    assert s[B]["precision"] == 1.0
    assert C not in s  # only fired on an open case
    assert c4


def test_stats_precision_math_and_null(client, conn, make_case):
    make_case(fired=("zz_api_rule_d",))
    decide(client, make_case(fired=("zz_api_rule_d",)), "false_positive")
    s = by_name(client)
    assert s["zz_api_rule_d"] == {
        "rule_name": "zz_api_rule_d", "confirmed_fraud": 0, "false_positive": 1, "precision": 0.0,
    }
    # rules_config rules are always listed; precision is null iff no decided cases fired them
    with conn.cursor() as cur:
        cur.execute("SELECT rule_name FROM rules_config")
        configured = [r[0] for r in cur.fetchall()]
    conn.rollback()
    for name in configured:
        e = s[name]
        denom = e["confirmed_fraud"] + e["false_positive"]
        assert e["precision"] == (e["confirmed_fraud"] / denom if denom else None)
