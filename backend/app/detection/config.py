"""Loads rules_config from Postgres into typed RuleConfig objects — the
read path Phase 4/5 will use to fetch current weights/thresholds. No
write/edit path here (that's Phase 5's API / Phase 9's UI).
"""
import psycopg2
import psycopg2.extras

from .models import RuleConfig


def load_rules_config(dsn: str) -> dict[str, RuleConfig]:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT rule_name, weight, enabled, params FROM rules_config;")
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        row["rule_name"]: RuleConfig(
            rule_name=row["rule_name"],
            weight=row["weight"],
            enabled=row["enabled"],
            params=row["params"],
        )
        for row in rows
    }
