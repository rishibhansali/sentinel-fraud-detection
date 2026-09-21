"""Real-Redis tests for publisher and both subscriber types (no mocks)."""
import asyncio
import json
import time
import uuid

import pytest
import redis

from app.realtime import publisher
from app.realtime.config import CASES_CHANNEL, RULES_CHANNEL, redis_url
from app.realtime.subscriber import AsyncChannelSubscriber, ThreadedChannelSubscriber

URL = redis_url()
DEAD_URL = "redis://localhost:6390/0"


@pytest.fixture(autouse=True)
def _reset():
    publisher.reset_publish_failure_count()
    yield
    publisher.reset_publish_failure_count()


@pytest.fixture
def channel():
    return f"test:{uuid.uuid4().hex}"


def _raw_publish(channel, payload):
    r = redis.Redis.from_url(URL)
    try:
        return r.publish(channel, payload)
    finally:
        r.close()


def _kill_pubsub_clients():
    r = redis.Redis.from_url(URL)
    try:
        r.execute_command("CLIENT", "KILL", "TYPE", "pubsub")
    finally:
        r.close()


def _wait(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


async def _await(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def test_config_defaults(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert redis_url() == "redis://localhost:6379/0"
    monkeypatch.setenv("REDIS_URL", "redis://x:1/2")
    assert redis_url() == "redis://x:1/2"
    assert CASES_CHANNEL == "cases:events" and RULES_CHANNEL == "rules:updated"


# ---- publisher failure behavior ------------------------------------------

def test_publish_unreachable_returns_false_fast(monkeypatch):
    monkeypatch.setenv("REDIS_URL", DEAD_URL)
    t0 = time.time()
    assert publisher.publish_case_event("case.created", {"id": 1}) is False
    assert publisher.publish_rules_updated(3) is False
    assert time.time() - t0 < 3
    assert publisher.publish_failure_count() == 2


def test_publish_recovers_after_dead_url(monkeypatch):
    monkeypatch.setenv("REDIS_URL", DEAD_URL)
    assert publisher.publish_rules_updated(1) is False
    monkeypatch.setenv("REDIS_URL", URL)
    assert publisher.publish_rules_updated(1) is True
    assert publisher.publish_failure_count() == 1


def test_publish_bad_event_type_never_raises():
    assert publisher.publish_case_event("bogus", {"id": 1}) is False


# ---- delivery -------------------------------------------------------------

def test_publish_delivered_to_threaded_subscriber():
    got = []
    sub = ThreadedChannelSubscriber(URL, CASES_CHANNEL, got.append)
    sub.start()
    try:
        assert sub.wait_ready(5)
        summary = {"id": 1, "status": "open"}
        assert publisher.publish_case_event("case.created", summary) is True
        assert _wait(lambda: got)
        assert got == [{"type": "case.created", "case": summary}]
    finally:
        sub.stop()


def test_publish_rules_updated_delivered():
    got = []
    sub = ThreadedChannelSubscriber(URL, RULES_CHANNEL, got.append)
    sub.start()
    try:
        assert sub.wait_ready(5)
        assert publisher.publish_rules_updated(42) is True
        assert _wait(lambda: got)
        assert got == [{"version": 42}]
    finally:
        sub.stop()


def test_publish_delivered_to_async_subscriber():
    summary = {"id": 2, "status": "open"}

    async def main():
        got = []
        sub = AsyncChannelSubscriber(URL, CASES_CHANNEL, got.append)
        task = asyncio.create_task(sub.run())
        await asyncio.wait_for(sub.wait_ready(), 5)
        assert await asyncio.to_thread(publisher.publish_case_event, "case.updated", summary) is True
        await _await(lambda: got)
        sub.stop()
        await asyncio.wait_for(task, 5)
        return got

    assert asyncio.run(main()) == [{"type": "case.updated", "case": summary}]


# ---- robustness -----------------------------------------------------------

def test_threaded_malformed_skipped_and_on_message_error_survives(channel):
    got = []

    def on_message(m):
        if m.get("boom"):
            raise RuntimeError("handler failure")
        got.append(m)

    sub = ThreadedChannelSubscriber(URL, channel, on_message)
    sub.start()
    try:
        assert sub.wait_ready(5)
        _raw_publish(channel, "{not json")
        _raw_publish(channel, json.dumps({"boom": True}))
        _raw_publish(channel, json.dumps({"ok": 1}))
        assert _wait(lambda: got)
        assert got == [{"ok": 1}]
    finally:
        sub.stop()


def test_async_malformed_skipped_and_on_message_error_survives(channel):
    async def main():
        got = []

        async def on_message(m):
            if m.get("boom"):
                raise RuntimeError("handler failure")
            got.append(m)

        sub = AsyncChannelSubscriber(URL, channel, on_message)
        task = asyncio.create_task(sub.run())
        await asyncio.wait_for(sub.wait_ready(), 5)
        await asyncio.to_thread(_raw_publish, channel, "{not json")
        await asyncio.to_thread(_raw_publish, channel, json.dumps({"boom": True}))
        await asyncio.to_thread(_raw_publish, channel, json.dumps({"ok": 1}))
        await _await(lambda: got)
        sub.stop()
        await asyncio.wait_for(task, 5)
        return got

    assert asyncio.run(main()) == [{"ok": 1}]


# ---- reconnect ------------------------------------------------------------

def test_threaded_reconnect_after_server_kill(channel):
    got, reconnects = [], []
    sub = ThreadedChannelSubscriber(URL, channel, got.append, on_reconnect=lambda: reconnects.append(1))
    sub.start()
    try:
        assert sub.wait_ready(5)
        assert reconnects == []  # first connect is not a reconnect
        _kill_pubsub_clients()
        assert _wait(lambda: reconnects, 10)
        _raw_publish(channel, json.dumps({"after": 1}))
        assert _wait(lambda: got, 10)
        assert got == [{"after": 1}]
    finally:
        sub.stop()


def test_async_reconnect_after_server_kill(channel):
    async def main():
        got, reconnects = [], []

        async def on_reconnect():
            reconnects.append(1)

        sub = AsyncChannelSubscriber(URL, channel, got.append, on_reconnect=on_reconnect)
        task = asyncio.create_task(sub.run())
        await asyncio.wait_for(sub.wait_ready(), 5)
        assert reconnects == []
        await asyncio.to_thread(_kill_pubsub_clients)
        assert await _await(lambda: reconnects)
        await asyncio.to_thread(_raw_publish, channel, json.dumps({"after": 1}))
        await _await(lambda: got)
        sub.stop()
        await asyncio.wait_for(task, 5)
        return got

    assert asyncio.run(main()) == [{"after": 1}]


def test_subscriber_with_dead_redis_retries_and_stops_cleanly(channel):
    sub = ThreadedChannelSubscriber(DEAD_URL, channel, lambda m: None)
    sub.start()
    time.sleep(0.5)
    assert not sub.wait_ready(0.1)
    sub.stop()
    assert not sub.is_alive()
