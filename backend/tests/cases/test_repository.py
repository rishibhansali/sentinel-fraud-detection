import random
import threading
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from app.cases.repository import (
    CaseConflict,
    CaseNotFound,
    claim_case,
    get_case,
    recompute_status,
    release_case,
    submit_feedback,
)
from tests.cases.conftest import DB_DSN


def _feedback_count(conn, cid):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM case_feedback WHERE case_id=%s", (cid,))
        n = cur.fetchone()[0]
    conn.commit()
    return n


# 1. claim
def test_get_case(conn, make_case):
    cid = make_case()
    c = get_case(conn, cid)
    assert c["id"] == cid and c["rule_results"] == {"r": 1}
    assert get_case(conn, cid + 10**9) is None


def test_claim_success(conn, make_case):
    cid = make_case()
    c = claim_case(conn, cid, "alice")
    assert c["status"] == "in_review" and c["claimed_by"] == "alice"
    assert c["claimed_at"] is not None
    assert get_case(conn, cid)["claimed_by"] == "alice"


def test_claim_twice_conflict(conn, make_case):
    cid = make_case()
    claim_case(conn, cid, "alice")
    with pytest.raises(CaseConflict) as e:
        claim_case(conn, cid, "bob")
    assert e.value.current_status == "in_review" and e.value.claimed_by == "alice"


def test_claim_unknown(conn):
    with pytest.raises(CaseNotFound):
        claim_case(conn, 10**12, "alice")


def test_claim_decided_conflict(conn, make_case):
    cid = make_case()
    submit_feedback(conn, cid, "alice", "false_positive")
    with pytest.raises(CaseConflict) as e:
        claim_case(conn, cid, "bob")
    assert e.value.current_status == "false_positive"


# 2. concurrency
def test_concurrent_claim_exactly_one_wins(conn, make_case):
    cid = make_case()
    n = 8
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        c = psycopg2.connect(DB_DSN)
        try:
            barrier.wait()
            try:
                claim_case(c, cid, f"analyst{i}")
                results[i] = "won"
            except CaseConflict:
                results[i] = "conflict"
        except Exception as ex:  # pragma: no cover
            results[i] = repr(ex)
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("won") == 1, results
    assert results.count("conflict") == n - 1, results
    winner = f"analyst{results.index('won')}"
    assert get_case(conn, cid)["claimed_by"] == winner


# 3. release
def test_release_rules(conn, make_case):
    cid = make_case()
    with pytest.raises(CaseConflict):
        release_case(conn, cid, "alice")  # open
    claim_case(conn, cid, "alice")
    before = get_case(conn, cid)
    with pytest.raises(CaseConflict):
        release_case(conn, cid, "bob")
    assert get_case(conn, cid) == before
    with pytest.raises(CaseNotFound):
        release_case(conn, 10**12, "alice")
    r = release_case(conn, cid, "alice")
    assert r["status"] == "open" and r["claimed_by"] is None and r["claimed_at"] is None
    assert claim_case(conn, cid, "bob")["claimed_by"] == "bob"


# 4. feedback policy
def test_feedback_policy(conn, make_case):
    cid = make_case()
    claim_case(conn, cid, "alice")
    with pytest.raises(CaseConflict):
        submit_feedback(conn, cid, "bob", "confirmed_fraud")
    assert _feedback_count(conn, cid) == 0
    r = submit_feedback(conn, cid, "alice", "confirmed_fraud", note="x")
    assert r["status"] == "confirmed_fraud" and r["claimed_by"] == "alice"
    # correction by a different analyst appends
    r = submit_feedback(conn, cid, "bob", "false_positive")
    assert r["status"] == "false_positive"
    assert _feedback_count(conn, cid) == 2


def test_feedback_on_open_ok(conn, make_case):
    cid = make_case()
    r = submit_feedback(conn, cid, "carol", "confirmed_fraud")
    assert r["status"] == "confirmed_fraud" and r["claimed_by"] is None


def test_feedback_invalid_and_unknown(conn, make_case):
    cid = make_case()
    with pytest.raises(ValueError):
        submit_feedback(conn, cid, "a", "maybe")
    with pytest.raises(CaseNotFound):
        submit_feedback(conn, 10**12, "a", "confirmed_fraud")
    assert _feedback_count(conn, cid) == 0
    assert get_case(conn, cid)["status"] == "open"


# 5. resolution rule
def _inject(conn, cid, decision, ts, analyst="inj"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO case_feedback (case_id, decision, analyst, decided_at) "
            "VALUES (%s,%s,%s,%s) RETURNING id",
            (cid, decision, analyst, ts),
        )
        return cur.fetchone()[0]


def test_resolution_tie_higher_id_wins(conn, make_case):
    cid = make_case()
    ts = datetime(2025, 6, 1, tzinfo=timezone.utc)
    _inject(conn, cid, "confirmed_fraud", ts)
    _inject(conn, cid, "false_positive", ts)
    with conn.cursor() as cur:
        assert recompute_status(cur, cid) == "false_positive"
    conn.commit()
    assert get_case(conn, cid)["status"] == "false_positive"


def test_resolution_later_decided_at_beats_insert_order(conn, make_case):
    cid = make_case()
    ts = datetime(2025, 6, 1, tzinfo=timezone.utc)
    _inject(conn, cid, "confirmed_fraud", ts + timedelta(hours=1))
    _inject(conn, cid, "false_positive", ts)  # inserted later, decided earlier
    with conn.cursor() as cur:
        assert recompute_status(cur, cid) == "confirmed_fraud"
    conn.commit()


# 7. audit retention
def test_decided_retains_claim_but_cannot_release_or_claim(conn, make_case):
    cid = make_case()
    claim_case(conn, cid, "alice")
    r = submit_feedback(conn, cid, "alice", "false_positive")
    assert r["claimed_by"] == "alice" and r["claimed_at"] is not None
    with pytest.raises(CaseConflict):
        release_case(conn, cid, "alice")
    with pytest.raises(CaseConflict):
        claim_case(conn, cid, "bob")
    assert get_case(conn, cid)["claimed_by"] == "alice"


# 6. projection property test
def _oracle(conn, cid):
    with conn.cursor() as cur:
        cur.execute("SELECT decision, decided_at, id FROM case_feedback WHERE case_id=%s", (cid,))
        fb = cur.fetchall()
        cur.execute("SELECT claimed_by FROM flagged_cases WHERE id=%s", (cid,))
        claimed_by = cur.fetchone()[0]
    conn.commit()
    if fb:
        return max(fb, key=lambda r: (r[1], r[2]))[0]
    return "in_review" if claimed_by is not None else "open"


def test_projection_property(conn, make_case):
    rng = random.Random(20260921)
    cases = [make_case() for _ in range(4)]
    analysts = ["alice", "bob", "carol"]
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    injected_ts = []
    steps = 260
    for step in range(steps):
        cid = rng.choice(cases)
        who = rng.choice(analysts)
        op = rng.choice(["claim", "release", "feedback", "feedback", "inject"])
        try:
            if op == "claim":
                claim_case(conn, cid, who)
            elif op == "release":
                release_case(conn, cid, who)
            elif op == "feedback":
                submit_feedback(conn, cid, who, rng.choice(["confirmed_fraud", "false_positive"]))
            else:
                if injected_ts and rng.random() < 0.4:
                    ts = rng.choice(injected_ts)  # tied timestamp
                else:
                    ts = base + timedelta(minutes=rng.randint(-500, 500))
                    injected_ts.append(ts)
                _inject(conn, cid, rng.choice(["confirmed_fraud", "false_positive"]), ts)
                with conn.cursor() as cur:
                    recompute_status(cur, cid)
                conn.commit()
        except (CaseConflict, CaseNotFound):
            pass  # expected policy rejections; a CHECK violation would be psycopg2.Error and fail
        for c in cases:
            assert get_case(conn, c)["status"] == _oracle(conn, c), (step, op, c)
