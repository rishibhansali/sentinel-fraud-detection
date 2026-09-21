"""Holds the rules configuration the pipeline scores against (spec 7.2, read
side).

The provider exposes one immutable snapshot, (rules_config, version), where
version = max(rules_config_history.id). Both are read inside a single
REPEATABLE READ transaction so they always describe the same moment, and the
snapshot is replaced by a single reference assignment, so a reader calling
current() sees either the old snapshot or the new one, never a mix. current()
takes no lock.

reload() is the single entry point for refreshing (serialized by a lock so two
triggers can never swap in an older snapshot after a newer one). Hot reload
(spec 7.2, start_hot_reload) has two triggers, both of which call reload():
a Redis `rules:updated` subscription (plus a forced reload on every
re-subscription, to repair the gap) and a safety-net poll of
max(rules_config_history.id). A failing reload is logged and never kills
either thread; the next poll retries.
"""
import logging
import threading
from dataclasses import dataclass
from typing import Optional

import psycopg2
import psycopg2.extras

from app.detection.models import RuleConfig
from app.realtime.config import RULES_CHANNEL, redis_url as default_redis_url
from app.realtime.subscriber import ThreadedChannelSubscriber

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RulesSnapshot:
    rules_config: dict[str, RuleConfig]
    version: Optional[int]


def load_snapshot(dsn: str) -> RulesSnapshot:
    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT rule_name, weight, enabled, params FROM rules_config;")
            rows = cur.fetchall()
            cur.execute("SELECT max(id) AS version FROM rules_config_history;")
            version = cur.fetchone()["version"]
        conn.rollback()
    finally:
        conn.close()
    rules = {
        row["rule_name"]: RuleConfig(
            rule_name=row["rule_name"],
            weight=row["weight"],
            enabled=row["enabled"],
            params=row["params"],
        )
        for row in rows
    }
    return RulesSnapshot(rules_config=rules, version=None if version is None else int(version))


def _max_history_id(dsn: str) -> Optional[int]:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT max(id) FROM rules_config_history;")
            v = cur.fetchone()[0]
        conn.rollback()
    finally:
        conn.close()
    return None if v is None else int(v)


class RulesProvider:
    def __init__(self, dsn: Optional[str], _snapshot: Optional[RulesSnapshot] = None):
        self._dsn = dsn
        self._snapshot = _snapshot if _snapshot is not None else load_snapshot(dsn)
        self._reload_lock = threading.Lock()
        self._subscriber: Optional[ThreadedChannelSubscriber] = None
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()

    @classmethod
    def static(cls, rules_config: dict[str, RuleConfig], version: Optional[int] = None) -> "RulesProvider":
        """Fixed snapshot, never touches the database (reload() is a no-op)."""
        return cls(None, _snapshot=RulesSnapshot(dict(rules_config), version))

    def current(self) -> RulesSnapshot:
        return self._snapshot

    def reload(self) -> RulesSnapshot:
        if self._dsn is None:
            return self._snapshot
        with self._reload_lock:
            snapshot = load_snapshot(self._dsn)
            self._snapshot = snapshot  # single reference assignment: atomic swap
            return snapshot

    # ---- hot reload (spec 7.2) ----

    def _safe_reload(self, why: str) -> None:
        try:
            self.reload()
        except Exception:  # noqa: BLE001 - must never kill a hot-reload thread
            log.exception("rules reload (%s) failed; keeping current snapshot", why)

    def _on_message(self, payload) -> None:
        version = payload.get("version") if isinstance(payload, dict) else None
        cur = self._snapshot.version
        if isinstance(version, int) and not isinstance(version, bool) and cur is not None and version <= cur:
            return  # already at (or past) this version
        self._safe_reload("rules:updated message")

    def _on_reconnect(self) -> None:
        # Messages may have been lost while disconnected: always reload.
        self._safe_reload("subscriber reconnect")

    def _poll_loop(self, interval: float) -> None:
        while not self._poll_stop.wait(interval):
            try:
                latest = _max_history_id(self._dsn)
                if latest != self._snapshot.version:
                    log.info("rules poll: version %s != %s, reloading", latest, self._snapshot.version)
                    self.reload()
            except Exception:  # noqa: BLE001
                log.exception("rules poll failed; will retry in %.1fs", interval)

    def start_hot_reload(self, redis_url: Optional[str] = None, poll_interval: float = 30.0) -> None:
        if self._dsn is None or self._poll_thread is not None:
            return  # static provider, or already started
        self._subscriber = ThreadedChannelSubscriber(
            redis_url or default_redis_url(), RULES_CHANNEL,
            on_message=self._on_message, on_reconnect=self._on_reconnect,
        )
        self._subscriber.start()
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, args=(poll_interval,), name="rules-poll", daemon=True
        )
        self._poll_thread.start()

    def stop_hot_reload(self) -> None:
        self._poll_stop.set()
        if self._subscriber is not None:
            self._subscriber.stop()
            self._subscriber = None
        if self._poll_thread is not None:
            self._poll_thread.join(5.0)
            self._poll_thread = None
