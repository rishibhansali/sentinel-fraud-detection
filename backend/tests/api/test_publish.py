import queue

import psycopg2
import pytest

from app.realtime.config import CASES_CHANNEL, redis_url
from app.realtime.subscriber import ThreadedChannelSubscriber
from tests.api.conftest import DB_DSN


@pytest.fixture
def events(make_case):
    """Real subscriber; on_message reads the DB on a SEPARATE connection and
    records what state was visible at the moment the event arrived."""
    q: queue.Queue = queue.Queue()
    reader = psycopg2.connect(DB_DSN)
    reader.autocommit = True

    def on_message(msg):
        case = msg["case"]
        with reader.cursor() as cur:
            cur.execute("SELECT status, claimed_by FROM flagged_cases WHERE id=%s", (case["id"],))
            row = cur.fetchone()
        q.put((msg, row))

    sub = ThreadedChannelSubscriber(redis_url(), CASES_CHANNEL, on_message)
    sub.start()
    assert sub.wait_ready(5)
    yield q
    sub.stop()
    reader.close()


def drain(q, case_ids, expected, timeout=5):
    got = []
    end = __import__("time").time() + timeout
    while len(got) < expected and __import__("time").time() < end:
        try:
            msg, row = q.get(timeout=0.2)
        except queue.Empty:
            continue
        if msg["case"]["id"] in case_ids:
            got.append((msg, row))
    return got


def test_mutations_publish_case_updated_after_commit(client, make_case, events):
    cid = make_case(priority=0.0)  # priority irrelevant here
    r = client.post(f"/cases/{cid}/claim", json={"analyst": "alice"})
    assert r.status_code == 200
    (msg, row), = drain(events, {cid}, 1)
    assert msg["type"] == "case.updated"
    assert msg["case"]["status"] == "in_review" and msg["case"]["claimed_by"] == "alice"
    assert msg["case"]["id"] == cid and msg["case"]["ml_anomaly_score"] is None
    assert row == ("in_review", "alice")  # committed state visible from another connection

    client.post(f"/cases/{cid}/release", json={"analyst": "alice"})
    (msg, row), = drain(events, {cid}, 1)
    assert msg["case"]["status"] == "open" and msg["case"]["claimed_by"] is None
    assert row == ("open", None)

    client.post(f"/cases/{cid}/claim", json={"analyst": "alice"})
    drain(events, {cid}, 1)
    client.post(f"/cases/{cid}/feedback", json={"analyst": "alice", "decision": "confirmed_fraud"})
    (msg, row), = drain(events, {cid}, 1)
    assert msg["case"]["status"] == "confirmed_fraud"
    assert row[0] == "confirmed_fraud"


def test_error_responses_publish_nothing(client, make_case, events):
    a = make_case()
    b = make_case()
    client.post(f"/cases/{a}/claim", json={"analyst": "alice"})
    drain(events, {a}, 1)
    # 409s, 404, 422: none may publish
    assert client.post(f"/cases/{a}/claim", json={"analyst": "bob"}).status_code == 409
    assert client.post(f"/cases/{a}/release", json={"analyst": "bob"}).status_code == 409
    assert client.post(f"/cases/{a}/feedback",
                       json={"analyst": "bob", "decision": "false_positive"}).status_code == 409
    assert client.post("/cases/999999999999/claim", json={"analyst": "x"}).status_code == 404
    assert client.post(f"/cases/{a}/feedback", json={"analyst": "a", "decision": "x"}).status_code == 422
    # Marker: a later success on another case arrives; pub/sub preserves order,
    # so anything wrongly published earlier would show up before it.
    client.post(f"/cases/{b}/claim", json={"analyst": "carol"})
    got = drain(events, {a, b}, 2, timeout=2)
    assert [m["case"]["id"] for m, _ in got] == [b]
