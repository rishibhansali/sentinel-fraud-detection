"""Pipeline hot reload against REAL Postgres and REAL Redis (spec 7.2)."""
import logging
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import pytest
import redis

from app.detection.models import Transaction
from app.pipeline.pipeline import make_pipeline_callback
from app.pipeline.rules_provider import RulesProvider
from app.realtime.config import redis_url
from tests.pipeline.reserved_ranges import RULES_TEST_TXN_BASE
from tests.rules.conftest import DB_DSN, direct_change, max_history_id

TS = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
USER_OLD, USER_A, USER_B = -5501, -5502, -5503  # user ids: RULES_TEST_USER_ID range -5501..-5510


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def patch(client, rule, **body):
    r = client.patch(f"/rules/{rule}", json={"changed_by": "hot-reload-test", **body})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def providers():
    made = []

    def make(**start_kwargs):
        p = RulesProvider(DB_DSN)
        p.start_hot_reload(**start_kwargs)
        made.append(p)
        return p

    yield make
    for p in made:
        p.stop_hot_reload()


def _insert_txn(conn, id_, user, ts):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO transactions (id, user_id, card_id, ts, amount, lat, lon,
                v1, v2, v3, v4, v5, v6, v7, v8, v9, v10, v11, v12, v13, v14, v15, v16, v17, v18, v19, v20,
                v21, v22, v23, v24, v25, v26, v27, v28, class, split)
               VALUES (%s, %s, 1, %s, 10.0, 0.0, 0.0,
                0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0, 0, 'test')""",
            (id_, user, ts),
        )
    conn.commit()


def _run_user(conn, on_txn, user, first_id, n):
    """n transactions for one user, 60s apart, scored in order. Returns the last id."""
    last = None
    for i in range(n):
        ts = TS + timedelta(seconds=60 * i)
        id_ = first_id + i
        _insert_txn(conn, id_, user, ts)
        on_txn(Transaction(id=id_, user_id=user, ts=ts, amount=10.0, lat=0.0, lon=0.0))
        last = id_
    return last


def _case(txn_id):
    c = psycopg2.connect(DB_DSN)
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM flagged_cases WHERE transaction_id=%s", (txn_id,))
            return cur.fetchone()
    finally:
        c.close()


def test_patch_changes_flagging_in_running_pipeline(client, providers, rules_guard):
    # Baseline: velocity threshold 5 (window 10 min).
    v_base = patch(client, "velocity", params={"threshold_count": 5, "window_minutes": 10})["version"]
    provider = providers(poll_interval=60)  # poll effectively off: only Redis can deliver
    assert provider.current().version == v_base
    conn = psycopg2.connect(DB_DSN)
    try:
        cb = make_pipeline_callback(conn, provider)
        # OLD: 5 txns in window -> last flags under threshold 5, stamped v_base.
        old_last = _run_user(conn, cb, USER_OLD, RULES_TEST_TXN_BASE + 0, 5)
        # A: 4 txns in window -> does NOT flag under threshold 5.
        a_last = _run_user(conn, cb, USER_A, RULES_TEST_TXN_BASE + 100, 4)
        assert _case(a_last) is None
        old_case = _case(old_last)
        assert old_case is not None and old_case["rules_config_version"] == v_base

        # PATCH via the real API; wait (bounded) for the running provider to swap.
        t0 = time.time()
        v_new = patch(client, "velocity", params={"threshold_count": 4})["version"]
        assert v_new > v_base
        assert wait_for(lambda: provider.current().version == v_new, 3.0)
        lag = time.time() - t0
        assert provider.current().rules_config["velocity"].params["threshold_count"] == 4

        # B: equivalent 4-txn sequence for another user now flags, stamped v_new.
        b_last = _run_user(conn, cb, USER_B, RULES_TEST_TXN_BASE + 200, 4)
        b_case = _case(b_last)
        assert b_case is not None
        assert b_case["rules_config_version"] == v_new
        assert old_case["rules_config_version"] == v_base  # earlier stamp unchanged
        print(f"\nHOT-RELOAD E2E: threshold 5 -> 4; version {v_base} -> {v_new} "
              f"(swap lag {lag * 1000:.0f} ms); txn {a_last} (4 in window) NOT flagged @v{v_base}; "
              f"txn {b_last} (4 in window) flagged, case {b_case['id']} stamped v{v_new}; "
              f"earlier case {old_case['id']} still stamped v{v_base}")
    finally:
        conn.close()


def test_poll_safety_net_when_redis_unreachable(providers):
    provider = providers(redis_url="redis://127.0.0.1:1/0", poll_interval=0.3)
    v0 = provider.current().version
    v1 = direct_change("velocity", params={"window_minutes": 10, "threshold_count": 8})
    assert v1 > v0 or v0 is None or v1 != v0
    assert wait_for(lambda: provider.current().version == v1, 3.0)
    assert provider.current().rules_config["velocity"].params["threshold_count"] == 8


def test_reconnect_repairs_after_client_kill(client, providers):
    provider = providers(poll_interval=600)
    assert provider._subscriber.wait_ready(5)
    r = redis.Redis.from_url(redis_url())
    r.execute_command("CLIENT", "KILL", "TYPE", "pubsub")
    v = patch(client, "velocity", weight=3.3)["version"]
    assert wait_for(lambda: provider.current().version == v, 5.0)
    assert provider.current().rules_config["velocity"].weight == 3.3


def test_message_with_old_version_is_skipped(providers):
    provider = providers(poll_interval=600)
    calls = []
    orig = provider.reload
    provider.reload = lambda: (calls.append(1), orig())[1]
    cur = provider.current().version
    provider._on_message({"version": cur})
    provider._on_message({"version": cur - 1})
    assert calls == []
    provider._on_message({"version": cur + 1})
    assert calls == [1]


def test_failing_reload_is_logged_and_threads_survive(providers, caplog):
    caplog.set_level(logging.WARNING)
    provider = providers(poll_interval=0.2)
    orig = provider.reload
    state = {"fails": 2}

    def flaky():
        if state["fails"] > 0:
            state["fails"] -= 1
            raise psycopg2.OperationalError("simulated db outage")
        return orig()

    provider.reload = flaky
    v = direct_change("velocity", weight=5.5)
    assert wait_for(lambda: state["fails"] == 0, 3.0)
    assert wait_for(lambda: provider.current().version == v, 3.0)
    assert any("poll failed" in r.getMessage() for r in caplog.records)
    assert provider._poll_thread.is_alive() and provider._subscriber.is_alive()


def test_failing_message_reload_logged_then_recovers(client, providers, caplog):
    caplog.set_level(logging.WARNING)
    provider = providers(poll_interval=0.5)
    good = provider._dsn
    provider._dsn = "postgresql://sentinel:wrong@localhost:5432/sentinel"  # bad DSN after start
    v = patch(client, "velocity", weight=6.5)["version"]
    assert wait_for(lambda: any("reload" in r.getMessage() and "failed" in r.getMessage()
                                for r in caplog.records), 3.0)
    assert provider.current().version != v
    assert provider._poll_thread.is_alive() and provider._subscriber.is_alive()
    provider._dsn = good
    assert wait_for(lambda: provider.current().version == v, 3.0)


def test_static_provider_hot_reload_is_noop():
    p = RulesProvider.static({}, version=3)
    p.start_hot_reload()
    assert p._subscriber is None and p._poll_thread is None
    p.stop_hot_reload()
    assert p.current().version == 3


def test_stop_stops_both_threads(providers):
    provider = providers(poll_interval=0.2)
    sub, poll = provider._subscriber, provider._poll_thread
    provider.stop_hot_reload()
    assert not sub.is_alive() and not poll.is_alive()
    assert provider._subscriber is None and provider._poll_thread is None


def test_guard_restores_state(client, rules_guard):
    patch(client, "velocity", weight=8.0)
    assert max_history_id() != rules_guard.max_id
    rules_guard.restore()
    rules_guard.assert_restored()
