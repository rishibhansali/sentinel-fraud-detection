"""Best-effort Redis publishing (spec 5.1). Publish NEVER raises.

Detection must not fail because Redis is down: any failure is logged,
counted, and reported as False. Short timeouts bound the stall a dead
Redis can cause in the synchronous pipeline.
"""
import json
import logging
import threading

import redis

from app.realtime.config import CASES_CHANNEL, RULES_CHANNEL, redis_url
from app.realtime.events import case_event

log = logging.getLogger(__name__)

_lock = threading.Lock()
_client: redis.Redis | None = None
_client_url: str | None = None
_failures = 0


def _get_client() -> redis.Redis:
    """Lazily create the client; recreate if REDIS_URL changed. The client's
    connection pool reconnects on its own after a dropped connection."""
    global _client, _client_url
    url = redis_url()
    with _lock:
        if _client is None or _client_url != url:
            if _client is not None:
                try:
                    _client.close()
                except Exception:
                    pass
            _client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
            _client_url = url
        return _client


def _publish(channel: str, message: dict) -> bool:
    global _failures
    try:
        _get_client().publish(channel, json.dumps(message))
        return True
    except Exception as exc:  # noqa: BLE001 - publish must never raise
        with _lock:
            _failures += 1
        log.warning("redis publish to %s failed: %s", channel, exc)
        return False


def publish_case_event(event_type: str, case_summary: dict) -> bool:
    global _failures
    try:
        message = case_event(event_type, case_summary)
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _failures += 1
        log.warning("could not build case event %r: %s", event_type, exc)
        return False
    return _publish(CASES_CHANNEL, message)


def publish_rules_updated(version: int) -> bool:
    return _publish(RULES_CHANNEL, {"version": version})


def publish_failure_count() -> int:
    return _failures


def reset_publish_failure_count() -> None:
    global _failures
    with _lock:
        _failures = 0
