"""Real DB/Redis proof that optional summaries follow rule-created cases."""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import pytest
from fastapi.testclient import TestClient

from app.detection.models import Transaction
from app.main import app
from app.pipeline.pipeline import make_pipeline_callback
from app.pipeline.rules_provider import RulesProvider
from app.realtime.config import CASES_CHANNEL, redis_url
from app.realtime.subscriber import ThreadedChannelSubscriber
from tests.pipeline.reserved_ranges import (
    SUMMARY_TEST_TXN_BASE as BASE,
    SUMMARY_TEST_TXN_MAX as MAX_ID,
    SUMMARY_TEST_USER_ID as USER,
)

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)
TS = datetime(2025, 6, 1, tzinfo=timezone.utc)
NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)


def _cleanup(conn):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM case_feedback WHERE case_id IN "
                    "(SELECT id FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s)",
                    (BASE, MAX_ID))
        cur.execute("DELETE FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s", (BASE, MAX_ID))
        cur.execute("DELETE FROM transactions WHERE id BETWEEN %s AND %s", (BASE, MAX_ID))
    conn.commit()


@pytest.fixture
def conn():
    connection = psycopg2.connect(DB_DSN)
    _cleanup(connection)
    try:
        yield connection
    finally:
        connection.rollback()
        _cleanup(connection)
        connection.close()


@pytest.fixture
def subscriber():
    messages = []
    visible = []

    def record(message):
        with psycopg2.connect(DB_DSN) as other:
            with other.cursor() as cur:
                cur.execute("SELECT ai_summary, ai_summary_model FROM flagged_cases WHERE id = %s",
                            (message["case"]["id"],))
                visible.append(cur.fetchone())
        messages.append(message)

    stream = ThreadedChannelSubscriber(redis_url(), CASES_CHANNEL, record)
    stream.start()
    assert stream.wait_ready(5)
    try:
        yield messages, visible
    finally:
        stream.stop()


def _insert(conn, id_, user, ts, amount, location):
    columns = ["id", "user_id", "card_id", "ts", "amount", "lat", "lon"]
    columns += [f"v{i}" for i in range(1, 29)]
    columns += ["class", "split"]
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO transactions ({', '.join(columns)}) "
            f"VALUES ({', '.join(['%s'] * len(columns))})",
            (id_, user, 1, ts, amount, *location, *([0] * 28), 0, "test"),
        )
    conn.commit()


def _pair(conn):
    _insert(conn, BASE + 1, USER, TS, 10, NYC)
    now = TS + timedelta(seconds=270)
    _insert(conn, BASE + 2, USER, now, 99, LONDON)
    return Transaction(BASE + 2, USER, now, 99, *LONDON)


def _case(id_):
    with psycopg2.connect(DB_DSN) as other:
        with other.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM flagged_cases WHERE transaction_id = %s", (id_,))
            return cur.fetchone()


def _events(messages, id_, count=1):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        found = [m for m in messages if m["case"]["transaction_id"] == id_]
        if len(found) >= count:
            return found
        time.sleep(0.02)
    return found


class StubSummarizer:
    model = "claude-haiku-4-5-20251001"

    def __init__(self, result="One recent transaction triggered the rules."):
        self.result = result
        self.calls = []

    def summarize(self, row, transaction):
        self.calls.append((row["id"], transaction.id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_summary_is_committed_before_created_event_and_duplicate_is_not_regenerated(conn, subscriber):
    messages, visible = subscriber
    transaction = _pair(conn)
    generator = StubSummarizer()
    callback = make_pipeline_callback(conn, RulesProvider(DB_DSN), case_summarizer=generator)
    callback(transaction)

    row = _case(transaction.id)
    assert row["ai_summary"] == generator.result
    assert row["ai_summary_model"] == generator.model
    assert row["ai_summary_generated_at"] is not None
    assert row["priority_score"] == row["total_score"]
    assert generator.calls == [(row["id"], transaction.id)]
    events = _events(messages, transaction.id)
    assert len(events) == 1 and events[0]["type"] == "case.created"
    assert events[0]["case"]["ai_summary"] == generator.result
    assert (generator.result, generator.model) in visible

    client = TestClient(app)
    detail = client.get(f"/cases/{row['id']}")
    assert detail.status_code == 200
    assert detail.json()["ai_summary"] == generator.result
    assert detail.json()["ai_summary_model"] == generator.model
    assert detail.json()["ai_summary_generated_at"] is not None
    since = client.get("/cases", params={"since_id": row["id"] - 1})
    assert since.status_code == 200
    item = next(item for item in since.json()["items"] if item["id"] == row["id"])
    assert item["ai_summary"] == generator.result
    assert item["ai_summary_model"] == generator.model

    callback(transaction)
    time.sleep(0.1)
    assert generator.calls == [(row["id"], transaction.id)]
    assert len(_events(messages, transaction.id)) == 1


@pytest.mark.parametrize("result", [RuntimeError("API unavailable"), "  ", "x" * 1001])
def test_failure_keeps_case_and_publishes_null_summary(conn, subscriber, caplog, result):
    messages, _ = subscriber
    transaction = _pair(conn)
    generator = StubSummarizer(result)
    with caplog.at_level(logging.WARNING, logger="app.pipeline.pipeline"):
        make_pipeline_callback(conn, RulesProvider(DB_DSN), case_summarizer=generator)(transaction)
    row = _case(transaction.id)
    assert row["ai_summary"] is None and row["ai_summary_model"] is None
    assert row["ai_summary_generated_at"] is None
    assert row["total_score"] > 0 and row["priority_score"] == row["total_score"]
    events = _events(messages, transaction.id)
    assert len(events) == 1 and events[0]["case"]["ai_summary"] is None
    assert any(str(row["id"]) in r.message for r in caplog.records)


def test_unflagged_and_disabled_cases_make_no_summary_request(conn):
    _insert(conn, BASE + 10, USER - 1, TS, 10, NYC)
    unflagged = Transaction(BASE + 10, USER - 1, TS, 10, *NYC)
    generator = StubSummarizer()
    make_pipeline_callback(conn, RulesProvider(DB_DSN), case_summarizer=generator)(unflagged)
    assert generator.calls == [] and _case(unflagged.id) is None

    flagged = _pair(conn)
    make_pipeline_callback(conn, RulesProvider(DB_DSN))(flagged)
    assert _case(flagged.id)["ai_summary"] is None
