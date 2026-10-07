"""Schema tests for migration 007 (case queue + feedback), real Postgres.

Rows inserted here use transaction ids / user_id reserved in
tests/pipeline/reserved_ranges.py and are removed after each test.
"""
import os

import psycopg2
import psycopg2.errors
import pytest

from tests.pipeline.reserved_ranges import SCHEMA_TEST_TXN_ID_BASE, SCHEMA_TEST_USER_ID

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)
TXN_MIN = SCHEMA_TEST_TXN_ID_BASE
TXN_MAX = SCHEMA_TEST_TXN_ID_BASE + 999


@pytest.fixture
def conn():
    c = psycopg2.connect(DB_DSN)
    c.autocommit = True
    _cleanup(c)
    yield c
    _cleanup(c)
    c.close()


def _cleanup(c):
    with c.cursor() as cur:
        cur.execute("SELECT to_regclass('case_feedback')")
        if cur.fetchone()[0] is not None:
            cur.execute(
                "DELETE FROM case_feedback WHERE case_id IN "
                "(SELECT id FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s)",
                (TXN_MIN, TXN_MAX),
            )
        cur.execute("DELETE FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s",
                    (TXN_MIN, TXN_MAX))


def _insert_case(c, n=1, **extra):
    cols = {
        "transaction_id": TXN_MIN + n,
        "transaction_ts": "2025-01-01T00:00:00Z",
        "user_id": SCHEMA_TEST_USER_ID,
        "total_score": 0.5,
        "rule_results": "{}",
        "priority_score": 0.5,
    }
    cols.update(extra)
    names = ", ".join(cols)
    ph = ", ".join(["%s"] * len(cols))
    with c.cursor() as cur:
        cur.execute(f"INSERT INTO flagged_cases ({names}) VALUES ({ph}) RETURNING id",
                    list(cols.values()))
        return cur.fetchone()[0]


def _columns(c, table):
    with c.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name=%s", (table,))
        return {r[0]: r[1:] for r in cur.fetchall()}


def test_flagged_cases_new_columns(conn):
    cols = _columns(conn, "flagged_cases")
    assert cols["status"][0] == "text" and cols["status"][1] == "NO"
    assert "open" in cols["status"][2]
    assert cols["claimed_by"][:2] == ("text", "YES")
    assert cols["claimed_at"][:2] == ("timestamp with time zone", "YES")
    assert cols["ml_anomaly_score"][:2] == ("double precision", "YES")
    assert cols["rules_config_version"][:2] == ("bigint", "YES")
    assert cols["priority_score"][:2] == ("double precision", "NO")
    assert cols["priority_adjustment"][:2] == ("jsonb", "YES")


def test_ml_anomaly_score_has_no_default_and_insert_leaves_null(conn):
    assert _columns(conn, "flagged_cases")["ml_anomaly_score"][2] is None
    cid = _insert_case(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT ml_anomaly_score, status, rules_config_version, "
                    "priority_adjustment FROM flagged_cases WHERE id=%s", (cid,))
        assert cur.fetchone() == (None, "open", None, None)


def test_priority_score_not_null(conn):
    with pytest.raises(psycopg2.errors.NotNullViolation):
        _insert_case(conn, priority_score=None)


@pytest.mark.parametrize("extra", [
    {"status": "bogus"},
    {"claimed_by": "alice"},                                   # claimed_at missing
    {"claimed_at": "2025-01-01T00:00:00Z"},                    # claimed_by missing
    {"status": "in_review"},                                   # no claimant
    {"status": "open", "claimed_by": "a", "claimed_at": "2025-01-01T00:00:00Z"},
])
def test_check_constraints_reject(conn, extra):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_case(conn, **extra)


def test_valid_in_review_row_accepted(conn):
    _insert_case(conn, status="in_review", claimed_by="a", claimed_at="2025-01-01T00:00:00Z")


def test_rules_config_version_fk(conn):
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _insert_case(conn, rules_config_version=-1)
    with conn.cursor() as cur:
        cur.execute("SELECT max(id) FROM rules_config_history")
        v = cur.fetchone()[0]
    _insert_case(conn, rules_config_version=v)


def test_case_feedback_columns(conn):
    cols = _columns(conn, "case_feedback")
    assert cols["case_id"][:2] == ("bigint", "NO")
    assert cols["decision"][:2] == ("text", "NO")
    assert cols["analyst"][:2] == ("text", "NO")
    assert cols["note"][:2] == ("text", "YES")
    assert cols["decided_at"][:2] == ("timestamp with time zone", "NO")
    assert "clock_timestamp" in cols["decided_at"][2]


def test_case_feedback_rejects_bad_decision(conn):
    cid = _insert_case(conn)
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("INSERT INTO case_feedback (case_id, decision, analyst) "
                        "VALUES (%s, 'maybe', 'a')", (cid,))


def test_case_feedback_rejects_orphan_case_id(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            cur.execute("INSERT INTO case_feedback (case_id, decision, analyst) "
                        "VALUES (-999999, 'confirmed_fraud', 'a')")


def test_case_feedback_accepts_valid_rows(conn):
    cid = _insert_case(conn)
    with conn.cursor() as cur:
        for d in ("confirmed_fraud", "false_positive"):
            cur.execute("INSERT INTO case_feedback (case_id, decision, analyst) "
                        "VALUES (%s, %s, 'a')", (cid, d))


def test_case_feedback_index(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE tablename='case_feedback'")
        defs = " ".join(r[0] for r in cur.fetchall())
    assert "(case_id, decided_at DESC, id DESC)" in defs


def test_flagged_cases_indexes(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes WHERE tablename='flagged_cases'")
        defs = [r[0] for r in cur.fetchall()]
    assert any("(status, priority_score DESC, id DESC)" in d for d in defs)
    for status in ("false_positive", "confirmed_fraud"):
        assert any("(user_id)" in d and f"status = '{status}'" in d for d in defs), status


def test_history_columns(conn):
    cols = _columns(conn, "rules_config_history")
    assert cols["id"][:2] == ("bigint", "NO")
    assert cols["rule_name"][:2] == ("text", "NO")
    assert cols["before"][:2] == ("jsonb", "YES")
    assert cols["after"][:2] == ("jsonb", "NO")
    assert cols["changed_by"][:2] == ("text", "NO")
    assert cols["changed_at"][:2] == ("timestamp with time zone", "NO")
    assert "clock_timestamp" in cols["changed_at"][2]


def test_history_seeded_one_row_per_rule(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT rule_name, weight, enabled, params FROM rules_config")
        rules = {r[0]: r[1:] for r in cur.fetchall()}
        cur.execute("SELECT rule_name, before, after, changed_by FROM rules_config_history "
                    "WHERE changed_by='migration' ORDER BY id")
        seeds = cur.fetchall()
    assert sorted(s[0] for s in seeds) == sorted(rules)
    for name, before, after, by in seeds:
        w, e, p = rules[name]
        assert before is None
        assert after == {"weight": w, "enabled": e, "params": p}


def test_existing_rows_backfilled_priority_equals_total(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM flagged_cases WHERE rules_config_version IS NULL "
                    "AND priority_score <> total_score AND transaction_id NOT BETWEEN %s AND %s",
                    (TXN_MIN, TXN_MAX))
        assert cur.fetchone()[0] == 0
