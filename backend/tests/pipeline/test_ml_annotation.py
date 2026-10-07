"""Real Postgres/Redis proof that optional ML never gates rule-created cases."""

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import pytest

from app.detection.models import Transaction
from app.pipeline.pipeline import make_pipeline_callback
from app.pipeline.rules_provider import RulesProvider
from app.realtime.config import CASES_CHANNEL, redis_url
from app.realtime.subscriber import ThreadedChannelSubscriber
from tests.pipeline.reserved_ranges import (
    ML_ANNOTATION_TEST_TXN_BASE as BASE,
    ML_ANNOTATION_TEST_TXN_MAX as MAX_ID,
    ML_ANNOTATION_TEST_USER_ID as USER,
)

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)
TS = datetime(2025, 6, 1, tzinfo=timezone.utc)
NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)


def _cleanup(conn):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM case_feedback WHERE case_id IN "
            "(SELECT id FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s)",
            (BASE, MAX_ID),
        )
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


def _insert(conn, id_, user, ts, location, amount, values):
    columns = ["id", "user_id", "card_id", "ts", "amount", "lat", "lon"]
    columns += [f"v{i}" for i in range(1, 29)]
    columns += ["class", "split"]
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO transactions ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
            (id_, user, 1, ts, amount, *location, *values, 0, "test"),
        )
    conn.commit()


def _case(id_):
    other = psycopg2.connect(DB_DSN)
    try:
        with other.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM flagged_cases WHERE transaction_id = %s", (id_,))
            return cur.fetchone()
    finally:
        other.close()


def _wait_for(messages, transaction_id, count=1):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        matching = [message for message in messages if message["case"]["transaction_id"] == transaction_id]
        if len(matching) >= count:
            return matching
        time.sleep(0.02)
    return matching


@pytest.fixture
def subscriber():
    messages = []
    visible_scores = []

    def record(message):
        other = psycopg2.connect(DB_DSN)
        try:
            with other.cursor() as cur:
                cur.execute("SELECT ml_anomaly_score FROM flagged_cases WHERE id = %s", (message["case"]["id"],))
                row = cur.fetchone()
                visible_scores.append((message["case"]["transaction_id"], row[0] if row else None))
        finally:
            other.close()
        messages.append(message)

    stream = ThreadedChannelSubscriber(redis_url(), CASES_CHANNEL, record)
    stream.start()
    assert stream.wait_ready(5)
    try:
        yield messages, visible_scores
    finally:
        stream.stop()


def _seed_pair(conn, current_id=BASE + 2):
    _insert(conn, BASE + 1, USER, TS, NYC, 10, [0] * 28)
    now = TS + timedelta(seconds=270)
    values = list(range(1, 29))
    _insert(conn, current_id, USER, now, LONDON, 99, values)
    return Transaction(id=current_id, user_id=USER, ts=now, amount=99, lat=LONDON[0], lon=LONDON[1])


def test_new_rule_case_gets_committed_score_before_event_and_duplicate_is_not_rescored(conn, subscriber):
    messages, visible_scores = subscriber
    current = _seed_pair(conn)
    scored = []

    def score(values, amount):
        scored.append((tuple(values), amount))
        return 0.42

    callback = make_pipeline_callback(conn, RulesProvider(DB_DSN), anomaly_scorer=score)
    callback(current)
    row = _case(current.id)
    assert row is not None
    assert row["ml_anomaly_score"] == pytest.approx(0.42)
    assert row["priority_score"] == row["total_score"]
    assert scored == [(tuple(range(1, 29)), 99)]
    events = _wait_for(messages, current.id)
    assert len(events) == 1
    assert events[0]["type"] == "case.created"
    assert events[0]["case"]["ml_anomaly_score"] == pytest.approx(0.42)
    assert (current.id, 0.42) in visible_scores

    callback(current)
    assert scored == [(tuple(range(1, 29)), 99)]
    time.sleep(0.2)
    assert len(_wait_for(messages, current.id)) == 1
    assert _case(current.id)["ml_anomaly_score"] == pytest.approx(0.42)


def test_unflagged_transaction_never_calls_model(conn, subscriber):
    messages, _ = subscriber
    id_ = BASE + 10
    user = USER - 1
    _insert(conn, id_, user, TS, NYC, 10, [0] * 28)

    def must_not_score(values, amount):
        raise AssertionError("model was called for an unflagged transaction")

    callback = make_pipeline_callback(conn, RulesProvider(DB_DSN), anomaly_scorer=must_not_score)
    callback(Transaction(id_, user, TS, 10, *NYC))
    assert _case(id_) is None
    assert _wait_for(messages, id_) == []


def test_annotation_uses_exact_timestamp_when_transaction_ids_repeat(conn):
    current_id = BASE + 2
    # The partitioned transactions PK is (id, ts), so this older row may
    # legally share the id. It must never supply the flagged case's features.
    _insert(
        conn, current_id, USER - 10, datetime(2025, 1, 1, tzinfo=timezone.utc),
        NYC, 1000, [-9] * 28,
    )
    current = _seed_pair(conn, current_id=current_id)
    scored = []

    def score(values, amount):
        scored.append((tuple(values), amount))
        return 0.42

    make_pipeline_callback(conn, RulesProvider(DB_DSN), anomaly_scorer=score)(current)
    assert _case(current.id)["ml_anomaly_score"] == pytest.approx(0.42)
    assert scored == [(tuple(range(1, 29)), 99)]


def test_model_error_keeps_case_and_publishes_null_score(conn, subscriber, caplog):
    messages, visible_scores = subscriber
    current = _seed_pair(conn)

    def broken(values, amount):
        raise RuntimeError("model failure")

    callback = make_pipeline_callback(conn, RulesProvider(DB_DSN), anomaly_scorer=broken)
    with caplog.at_level(logging.WARNING, logger="app.pipeline.pipeline"):
        callback(current)
    row = _case(current.id)
    assert row is not None
    assert row["ml_anomaly_score"] is None
    assert row["total_score"] > 0 and row["priority_score"] == row["total_score"]
    events = _wait_for(messages, current.id)
    assert len(events) == 1 and events[0]["case"]["ml_anomaly_score"] is None
    assert (current.id, None) in visible_scores
    assert any(str(row["id"]) in r.message and str(current.id) in r.message for r in caplog.records)
