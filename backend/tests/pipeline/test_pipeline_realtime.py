"""Phase 5 Task 4: pipeline integration against REAL Postgres and REAL Redis
(no mocks): ON CONFLICT decision, version stamping, publish-after-commit,
Redis-down resilience.
"""
import dataclasses
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import pytest

from app.detection.config import load_rules_config
from app.detection.models import Transaction
from app.detection.scoring import score_transaction
from app.pipeline.loader import get_recent_transactions, get_user_baseline
from app.pipeline.pipeline import make_pipeline_callback, persist_flagged_case
from app.pipeline.rules_provider import RulesProvider
from app.realtime import publisher
from app.realtime.config import CASES_CHANNEL, redis_url
from app.realtime.subscriber import ThreadedChannelSubscriber
from tests.pipeline.reserved_ranges import (
    PIPELINE_RT_TEST_TXN_BASE,
    PIPELINE_RT_TEST_TXN_MAX,
    PIPELINE_RT_TEST_USER_ID,
)

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)
TS = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)
BASE = PIPELINE_RT_TEST_TXN_BASE
USER = PIPELINE_RT_TEST_USER_ID


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _insert_txn(conn, id_, ts, lat, lon):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (id, user_id, card_id, ts, amount, lat, lon,
                v1, v2, v3, v4, v5, v6, v7, v8, v9, v10, v11, v12, v13, v14, v15, v16, v17, v18, v19, v20,
                v21, v22, v23, v24, v25, v26, v27, v28, class, split)
            VALUES (%s, %s, 1, %s, 10.0, %s, %s,
                0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0, 0, 'test')
            """,
            (id_, USER, ts, lat, lon),
        )
    conn.commit()


def _txn(id_, ts, lat, lon):
    return Transaction(id=id_, user_id=USER, ts=ts, amount=10.0, lat=lat, lon=lon)


def _cleanup(c):
    with c.cursor() as cur:
        cur.execute(
            "DELETE FROM case_feedback WHERE case_id IN "
            "(SELECT id FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s)",
            (BASE, PIPELINE_RT_TEST_TXN_MAX),
        )
        cur.execute("DELETE FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s", (BASE, PIPELINE_RT_TEST_TXN_MAX))
        cur.execute("DELETE FROM transactions WHERE id BETWEEN %s AND %s", (BASE, PIPELINE_RT_TEST_TXN_MAX))
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


def _seed_geo_pair(conn):
    """Prior in NYC, current in London 270s later: geo_impossibility fires."""
    _insert_txn(conn, BASE, TS, *NYC)
    ts2 = TS + timedelta(seconds=270)
    _insert_txn(conn, BASE + 1, ts2, *LONDON)
    return _txn(BASE + 1, ts2, *LONDON)


def _case_row(txn_id):
    c = psycopg2.connect(DB_DSN)
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM flagged_cases WHERE transaction_id = %s", (txn_id,))
            return cur.fetchone()
    finally:
        c.close()


def _max_version():
    c = psycopg2.connect(DB_DSN)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT max(id) FROM rules_config_history")
            return cur.fetchone()[0]
    finally:
        c.close()


@pytest.fixture
def subscriber():
    subs = []

    def make(on_message):
        s = ThreadedChannelSubscriber(redis_url(), CASES_CHANNEL, on_message)
        s.start()
        assert s.wait_ready(5)
        subs.append(s)
        return s

    yield make
    for s in subs:
        s.stop()


def test_rules_provider_snapshot_is_consistent_and_versioned():
    provider = RulesProvider(DB_DSN)
    snap = provider.current()
    assert snap.rules_config.keys() == load_rules_config(DB_DSN).keys()
    assert snap.version == _max_version()
    assert provider.current() is snap


def test_rules_provider_reload_bumps_version_after_history_insert():
    provider = RulesProvider(DB_DSN)
    before = provider.current()
    c = psycopg2.connect(DB_DSN)
    new_id = None
    try:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO rules_config_history (rule_name, before, after, changed_by) "
                "VALUES ('velocity', NULL, '{}'::jsonb, 'test_pipeline_realtime') RETURNING id"
            )
            new_id = cur.fetchone()[0]
        c.commit()
        assert provider.current() is before  # not reloaded yet
        provider.reload()
        assert provider.current().version == new_id
        assert before.version != new_id  # old snapshot object untouched
    finally:
        if new_id is not None:
            with c.cursor() as cur:
                cur.execute("DELETE FROM rules_config_history WHERE id = %s", (new_id,))
            c.commit()
        c.close()


def test_static_provider():
    cfg = load_rules_config(DB_DSN)
    p = RulesProvider.static(cfg, version=7)
    assert p.current().rules_config == cfg and p.current().version == 7
    assert RulesProvider.static(cfg).current().version is None


def test_flagged_case_stamped_with_version_priority_and_null_ml(conn):
    current = _seed_geo_pair(conn)
    provider = RulesProvider(DB_DSN)
    make_pipeline_callback(conn, provider)(current)
    row = _case_row(current.id)
    assert row is not None
    assert row["rules_config_version"] == provider.current().version == _max_version()
    assert row["priority_score"] == row["total_score"]
    assert row["ml_anomaly_score"] is None


def test_dict_still_accepted_for_backward_compat(conn):
    current = _seed_geo_pair(conn)
    make_pipeline_callback(conn, load_rules_config(DB_DSN))(current)
    row = _case_row(current.id)
    assert row is not None and row["rules_config_version"] is None


def test_persist_returns_id_then_none(conn):
    current = _seed_geo_pair(conn)
    cfg = load_rules_config(DB_DSN)
    res = score_transaction(
        current, get_recent_transactions(conn, USER, current.ts, current.id, 100),
        get_user_baseline(conn, USER), cfg,
    )
    first = persist_flagged_case(conn, current, res, rules_config_version=_max_version())
    assert isinstance(first, int)
    assert persist_flagged_case(conn, current, res, rules_config_version=_max_version()) is None
    conn.commit()


def test_duplicate_is_skipped_logged_and_not_republished(conn, subscriber, caplog):
    got: list = []
    subscriber(got.append)
    current = _seed_geo_pair(conn)
    cfg = load_rules_config(DB_DSN)
    version = _max_version()
    make_pipeline_callback(conn, RulesProvider.static(cfg, version))(current)
    assert _wait(lambda: len(got) == 1)
    stored = _case_row(current.id)
    assert stored["rules_config_version"] == version

    # total_score is weight-normalized, so change one rule's weight only.
    heavier = dict(cfg)
    heavier["geo_impossibility"] = dataclasses.replace(cfg["geo_impossibility"], weight=cfg["geo_impossibility"].weight * 10)
    with caplog.at_level(logging.WARNING, logger="app.pipeline.pipeline"):
        make_pipeline_callback(conn, RulesProvider.static(heavier, None))(current)
    after = _case_row(current.id)
    for col in ("id", "total_score", "rule_results", "rules_config_version", "flagged_at", "priority_score"):
        assert after[col] == stored[col], col
    time.sleep(0.5)
    assert len(got) == 1  # no second event
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(stored["total_score"]) in m and "duplicate" in m.lower() for m in msgs)
    new_score = score_transaction(
        current, get_recent_transactions(conn, USER, current.ts, current.id, 100),
        get_user_baseline(conn, USER), heavier,
    ).total_score
    assert new_score != stored["total_score"]
    assert any(str(new_score) in m for m in msgs)


def test_event_published_after_commit_with_summary(conn, subscriber):
    visible: list = []
    got: list = []

    def on_message(msg):
        c = psycopg2.connect(DB_DSN)  # separate connection
        try:
            with c.cursor() as cur:
                cur.execute("SELECT id FROM flagged_cases WHERE transaction_id = %s", (msg["case"]["transaction_id"],))
                visible.append(cur.fetchone())
        finally:
            c.close()
        got.append(msg)

    subscriber(on_message)
    current = _seed_geo_pair(conn)
    make_pipeline_callback(conn, RulesProvider(DB_DSN))(current)
    assert _wait(lambda: len(got) == 1)
    row = _case_row(current.id)
    assert visible == [(row["id"],)]
    assert got[0]["type"] == "case.created"
    case = got[0]["case"]
    assert case["id"] == row["id"] and case["transaction_id"] == current.id
    assert case["user_id"] == USER and case["status"] == "open"
    assert case["total_score"] == row["total_score"] and case["priority_score"] == row["priority_score"]
    assert "geo_impossibility" in case["fired_rules"]
    assert case["ml_anomaly_score"] is None and case["claimed_by"] is None


def test_no_event_for_unflagged_transaction(conn, subscriber):
    got: list = []
    subscriber(got.append)
    _insert_txn(conn, BASE + 50, TS, 0.0, 0.0)
    make_pipeline_callback(conn, RulesProvider(DB_DSN))(_txn(BASE + 50, TS, 0.0, 0.0))
    time.sleep(0.5)
    assert got == []
    assert _case_row(BASE + 50) is None


def test_redis_unreachable_pipeline_still_persists(conn, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6390/0")
    current = _seed_geo_pair(conn)
    make_pipeline_callback(conn, RulesProvider(DB_DSN))(current)  # must not raise
    assert _case_row(current.id) is not None
    assert publisher.publish_failure_count() >= 1
