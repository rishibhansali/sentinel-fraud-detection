"""Shared fixtures for the rules admin API / hot reload tests.

These tests mutate the REAL, shared `rules_config` and append to
`rules_config_history`. The autouse `rules_guard` fixture snapshots both
before each test and ALWAYS restores them afterwards: it deletes any
flagged_cases/case_feedback referencing history ids we created, restores
rules_config rows to their exact original values (incl. updated_at), deletes
ONLY history rows with id > the saved max, and then asserts equality.
"""
import json
import os

import psycopg2
import psycopg2.extras
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.pipeline.reserved_ranges import RULES_TEST_TXN_BASE, RULES_TEST_TXN_MAX

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)


def fetch_rules():
    c = psycopg2.connect(DB_DSN)
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM rules_config ORDER BY rule_name")
            return cur.fetchall()
    finally:
        c.close()


def max_history_id():
    c = psycopg2.connect(DB_DSN)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT max(id) FROM rules_config_history")
            return cur.fetchone()[0]
    finally:
        c.close()


def history_rows(after_id):
    c = psycopg2.connect(DB_DSN)
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM rules_config_history WHERE id > %s ORDER BY id", (after_id,))
            return cur.fetchall()
    finally:
        c.close()


class Guard:
    def __init__(self):
        self.rules = fetch_rules()
        self.max_id = max_history_id()

    def restore(self):
        c = psycopg2.connect(DB_DSN)
        try:
            with c.cursor() as cur:
                cur.execute(
                    "DELETE FROM case_feedback WHERE case_id IN "
                    "(SELECT id FROM flagged_cases WHERE rules_config_version > %s "
                    " OR transaction_id BETWEEN %s AND %s)",
                    (self.max_id, RULES_TEST_TXN_BASE, RULES_TEST_TXN_MAX),
                )
                cur.execute(
                    "DELETE FROM flagged_cases WHERE rules_config_version > %s "
                    "OR transaction_id BETWEEN %s AND %s",
                    (self.max_id, RULES_TEST_TXN_BASE, RULES_TEST_TXN_MAX),
                )
                cur.execute("DELETE FROM transactions WHERE id BETWEEN %s AND %s",
                            (RULES_TEST_TXN_BASE, RULES_TEST_TXN_MAX))
                for r in self.rules:
                    cur.execute(
                        "UPDATE rules_config SET weight=%s, enabled=%s, params=%s, updated_at=%s "
                        "WHERE rule_name=%s",
                        (r["weight"], r["enabled"], json.dumps(r["params"]), r["updated_at"], r["rule_name"]),
                    )
                cur.execute("DELETE FROM rules_config_history WHERE id > %s", (self.max_id,))
            c.commit()
        finally:
            c.close()

    def assert_restored(self):
        assert fetch_rules() == self.rules
        assert max_history_id() == self.max_id


@pytest.fixture(autouse=True)
def rules_guard():
    g = Guard()
    try:
        yield g
    finally:
        g.restore()
        g.assert_restored()


@pytest.fixture(scope="module")
def client():
    with TestClient(create_app()) as c:
        yield c


def direct_change(rule_name, changed_by="direct", **fields):
    """Change a rule the way the API would (UPDATE + history row, no publish)."""
    c = psycopg2.connect(DB_DSN)
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT weight, enabled, params FROM rules_config WHERE rule_name=%s FOR UPDATE",
                        (rule_name,))
            before = cur.fetchone()
            after = {**before, **fields}
            cur.execute(
                "UPDATE rules_config SET weight=%s, enabled=%s, params=%s, updated_at=clock_timestamp() "
                "WHERE rule_name=%s",
                (after["weight"], after["enabled"], json.dumps(after["params"]), rule_name),
            )
            cur.execute(
                "INSERT INTO rules_config_history (rule_name, before, after, changed_by) "
                "VALUES (%s,%s,%s,%s) RETURNING id",
                (rule_name, json.dumps(before), json.dumps(after), changed_by),
            )
            vid = cur.fetchone()["id"]
        c.commit()
        return vid
    finally:
        c.close()
