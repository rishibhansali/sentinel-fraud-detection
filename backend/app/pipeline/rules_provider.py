"""Holds the rules configuration the pipeline scores against (spec 7.2, read
side).

The provider exposes one immutable snapshot, (rules_config, version), where
version = max(rules_config_history.id). Both are read inside a single
REPEATABLE READ transaction so they always describe the same moment, and the
snapshot is replaced by a single reference assignment, so a reader calling
current() sees either the old snapshot or the new one, never a mix. current()
takes no lock.

reload() is the single entry point for refreshing. The Redis `rules:updated`
subscription and the 30s version poll (Task 7) are meant to call it.
"""
from dataclasses import dataclass
from typing import Optional

import psycopg2
import psycopg2.extras

from app.detection.models import RuleConfig


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


class RulesProvider:
    def __init__(self, dsn: Optional[str], _snapshot: Optional[RulesSnapshot] = None):
        self._dsn = dsn
        self._snapshot = _snapshot if _snapshot is not None else load_snapshot(dsn)

    @classmethod
    def static(cls, rules_config: dict[str, RuleConfig], version: Optional[int] = None) -> "RulesProvider":
        """Fixed snapshot, never touches the database (reload() is a no-op)."""
        return cls(None, _snapshot=RulesSnapshot(dict(rules_config), version))

    def current(self) -> RulesSnapshot:
        return self._snapshot

    def reload(self) -> RulesSnapshot:
        if self._dsn is None:
            return self._snapshot
        snapshot = load_snapshot(self._dsn)
        self._snapshot = snapshot  # single reference assignment: atomic swap
        return snapshot
