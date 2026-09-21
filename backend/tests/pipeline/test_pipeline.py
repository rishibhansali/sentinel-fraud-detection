"""Tests for the pipeline wiring (is_flagged, persist_flagged_case,
make_pipeline_callback). The end-to-end test runs the real ReplayHarness
against a real Postgres instance (no mocks), seeding a deterministic mix of
three independent users so exactly two of eight transactions should flag.
"""
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from app.detection.config import load_rules_config
from app.detection.geo import haversine_distance_km
from app.detection.models import RuleResult, ScoreResult
from app.pipeline.pipeline import is_flagged, make_pipeline_callback
from app.pipeline.replay import ReplayHarness

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)

TS = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)

TEST_ID_BASE = 7_000_000
USER_GEO = -3001
USER_VELOCITY = -3002
USER_CLEAN = -3003


def test_is_flagged_true_when_any_rule_fired():
    result = ScoreResult(total_score=0.5, rule_results=[
        RuleResult(rule_name="velocity", fired=False, sub_score=0.2, details={}),
        RuleResult(rule_name="amount_baseline", fired=True, sub_score=4.0, details={}),
        RuleResult(rule_name="geo_impossibility", fired=False, sub_score=0.0, details={}),
    ])
    assert is_flagged(result) is True


def test_is_flagged_false_when_no_rule_fired():
    result = ScoreResult(total_score=0.5, rule_results=[
        RuleResult(rule_name="velocity", fired=False, sub_score=0.2, details={}),
        RuleResult(rule_name="amount_baseline", fired=False, sub_score=0.5, details={}),
        RuleResult(rule_name="geo_impossibility", fired=False, sub_score=0.0, details={}),
    ])
    assert is_flagged(result) is False


def _insert_transaction(conn, id_: int, user_id: int, ts: datetime, lat: float, lon: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (
                id, user_id, card_id, ts, amount, lat, lon,
                v1, v2, v3, v4, v5, v6, v7, v8, v9, v10,
                v11, v12, v13, v14, v15, v16, v17, v18, v19, v20,
                v21, v22, v23, v24, v25, v26, v27, v28,
                class, split
            ) VALUES (
                %s, %s, 1, %s, 10.0, %s, %s,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0,
                0, 'test'
            );
            """,
            (id_, user_id, ts, lat, lon),
        )
    conn.commit()


@pytest.fixture
def seeded_conn():
    conn = psycopg2.connect(DB_DSN)
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE id >= %s;", (TEST_ID_BASE,))
            cur.execute("DELETE FROM flagged_cases WHERE transaction_id >= %s;", (TEST_ID_BASE,))
        conn.commit()
        conn.close()


def test_end_to_end_pipeline_flags_correct_transactions(seeded_conn):
    # geo user: prior in NYC, current in London 270s later -> geo fires.
    geo_prior_id = TEST_ID_BASE + 1
    geo_current_id = TEST_ID_BASE + 7
    _insert_transaction(seeded_conn, geo_prior_id, USER_GEO, TS, NYC[0], NYC[1])
    _insert_transaction(seeded_conn, geo_current_id, USER_GEO, TS + timedelta(seconds=270), LONDON[0], LONDON[1])

    # velocity user: 5 transactions inside the 10-minute window -> 5th fires.
    velocity_ids = [TEST_ID_BASE + 2, TEST_ID_BASE + 3, TEST_ID_BASE + 4, TEST_ID_BASE + 6, TEST_ID_BASE + 8]
    velocity_offsets = [30, 90, 150, 210, 280]
    for id_, offset in zip(velocity_ids, velocity_offsets):
        _insert_transaction(seeded_conn, id_, USER_VELOCITY, TS + timedelta(seconds=offset), 0.0, 0.0)
    velocity_current_id = velocity_ids[-1]

    # clean user: single transaction, no history -> nothing fires.
    clean_id = TEST_ID_BASE + 5
    _insert_transaction(seeded_conn, clean_id, USER_CLEAN, TS + timedelta(seconds=200), 0.0, 0.0)

    all_ids = [geo_prior_id] + velocity_ids[:3] + [clean_id, velocity_ids[3], geo_current_id, velocity_ids[4]]
    assert sorted(all_ids) == all_ids  # sanity: id order == ts order, matching Task 2's invariant

    rules_config = load_rules_config(DB_DSN)
    processing_conn = psycopg2.connect(DB_DSN)
    try:
        on_transaction = make_pipeline_callback(processing_conn, rules_config)
        harness = ReplayHarness(
            conn_factory=lambda: psycopg2.connect(DB_DSN),
            speed_multiplier=10000.0,
            start_id=TEST_ID_BASE,
            end_id=TEST_ID_BASE + 8,
        )
        harness.start(on_transaction)
        harness.join(timeout=10)
    finally:
        processing_conn.close()

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT transaction_id, total_score, rule_results FROM flagged_cases "
            "WHERE transaction_id >= %s ORDER BY transaction_id;",
            (TEST_ID_BASE,),
        )
        rows = cur.fetchall()

    flagged_ids = [r[0] for r in rows]
    assert flagged_ids == [geo_current_id, velocity_current_id]

    by_id = {r[0]: (r[1], r[2]) for r in rows}

    # --- geo row ---
    geo_total_score, geo_rule_results = by_id[geo_current_id]
    geo_by_rule = {r["rule_name"]: r for r in geo_rule_results}
    assert geo_by_rule["geo_impossibility"]["fired"] is True
    assert geo_by_rule["velocity"]["fired"] is False
    assert geo_by_rule["amount_baseline"]["fired"] is False

    expected_distance_km = haversine_distance_km(NYC[0], NYC[1], LONDON[0], LONDON[1])
    expected_elapsed_hours = 270 / 3600.0
    expected_speed = expected_distance_km / expected_elapsed_hours
    expected_geo_subscore = expected_speed / 900.0  # seeded max_speed_kmh
    expected_velocity_subscore = 2 / 5  # 1 prior in window + current, threshold 5
    expected_amount_subscore = 0.0  # zero baseline, never seeded/refreshed for this user
    expected_geo_total = (expected_velocity_subscore + expected_amount_subscore + expected_geo_subscore) / 3.0

    assert geo_by_rule["geo_impossibility"]["sub_score"] == pytest.approx(expected_geo_subscore)
    assert geo_by_rule["velocity"]["sub_score"] == pytest.approx(expected_velocity_subscore)
    assert geo_total_score == pytest.approx(expected_geo_total)

    # --- velocity row ---
    velocity_total_score, velocity_rule_results = by_id[velocity_current_id]
    velocity_by_rule = {r["rule_name"]: r for r in velocity_rule_results}
    assert velocity_by_rule["velocity"]["fired"] is True
    assert velocity_by_rule["geo_impossibility"]["fired"] is False  # lat/lon constant -> below min_distance_km guard
    assert velocity_by_rule["amount_baseline"]["fired"] is False

    expected_velocity_current_subscore = 5 / 5  # 4 prior in window + current, threshold 5
    expected_velocity_total = (expected_velocity_current_subscore + 0.0 + 0.0) / 3.0

    assert velocity_by_rule["velocity"]["sub_score"] == pytest.approx(expected_velocity_current_subscore)
    assert velocity_total_score == pytest.approx(expected_velocity_total)
