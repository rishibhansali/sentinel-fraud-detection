"""End-to-end determinism and explainability tests for score_transaction(),
covering the five confirmed Phase 3 decisions and the two no-history edge
cases together, through realistic multi-rule scenarios rather than
single-rule isolation (Tasks 2-3 already cover those in isolation).
"""
from datetime import datetime, timedelta, timezone

from app.detection.models import RuleConfig, Transaction, UserBaseline
from app.detection.scoring import score_transaction

TS = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)


def _config(velocity_enabled=True, amount_enabled=True, geo_enabled=True) -> dict[str, RuleConfig]:
    return {
        "velocity": RuleConfig("velocity", weight=1.0, enabled=velocity_enabled,
                                params={"window_minutes": 10, "threshold_count": 5}),
        "amount_baseline": RuleConfig("amount_baseline", weight=1.0, enabled=amount_enabled,
                                       params={"deviation_multiplier": 3.0}),
        "geo_impossibility": RuleConfig("geo_impossibility", weight=1.0, enabled=geo_enabled,
                                         params={"min_distance_km": 50.0, "max_speed_kmh": 900.0}),
    }


def _prior(minutes_ago: int, lat=0.0, lon=0.0, amount=100.0, id_=1) -> Transaction:
    return Transaction(id=id_, user_id=1, ts=TS - timedelta(minutes=minutes_ago),
                        amount=amount, lat=lat, lon=lon)


def test_identical_inputs_produce_identical_scores():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=500.0, lat=LONDON[0], lon=LONDON[1])
    recent = [_prior(1), _prior(2), _prior(3), _prior(4)]
    baseline = UserBaseline(transaction_count=10, avg_amount=100.0, total_amount=1000.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))
    config = _config()

    result1 = score_transaction(txn, recent, baseline, config)
    result2 = score_transaction(txn, recent, baseline, config)

    assert result1 == result2


def test_explainability_all_three_rules_fire_with_exact_details():
    # Velocity: 4 prior in-window + current = 5 >= threshold 5 -> fires.
    # Amount: 500 vs avg 100 -> relative_deviation 4.0 >= 3.0 -> fires.
    # Geo: previous transaction in NYC, current in London 1 minute later -> fires.
    recent = [
        Transaction(id=1, user_id=1, ts=TS - timedelta(minutes=1), amount=100.0, lat=NYC[0], lon=NYC[1]),
        _prior(2), _prior(3), _prior(4),
    ]
    txn = Transaction(id=99, user_id=1, ts=TS, amount=500.0, lat=LONDON[0], lon=LONDON[1])
    baseline = UserBaseline(transaction_count=10, avg_amount=100.0, total_amount=1000.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))

    result = score_transaction(txn, recent, baseline, _config())

    fired_rules = {r.rule_name for r in result.rule_results if r.fired}
    assert fired_rules == {"velocity", "amount_baseline", "geo_impossibility"}

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    assert velocity_result.details["count_in_window"] == 5

    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    assert amount_result.details["relative_deviation"] == 4.0

    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert geo_result.details["implied_speed_kmh"] > 900.0


def test_decision_zero_enabled_rules():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=500.0, lat=LONDON[0], lon=LONDON[1])
    baseline = UserBaseline(transaction_count=10, avg_amount=100.0, total_amount=1000.0,
                             fraud_count=0, last_transaction_at=TS)
    config = _config(velocity_enabled=False, amount_enabled=False, geo_enabled=False)

    result = score_transaction(txn, [], baseline, config)

    assert result.total_score == 0.0
    assert len(result.rule_results) == 3
    assert all(r.fired is False for r in result.rule_results)


def test_decision_geo_zero_elapsed_time_automatic_fire():
    previous = Transaction(id=1, user_id=1, ts=TS, amount=100.0, lat=NYC[0], lon=NYC[1])
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=LONDON[0], lon=LONDON[1])
    baseline = UserBaseline(transaction_count=1, avg_amount=100.0, total_amount=100.0,
                             fraud_count=0, last_transaction_at=TS)

    result = score_transaction(txn, [previous], baseline, _config())

    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert geo_result.fired is True
    assert geo_result.details["elapsed_hours"] == 0.0


def test_decision_disabled_rule_never_omitted():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=1, avg_amount=100.0, total_amount=100.0,
                             fraud_count=0, last_transaction_at=TS)

    result = score_transaction(txn, [], baseline, _config(velocity_enabled=False))

    assert len(result.rule_results) == 3
    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    assert velocity_result.details == {"skipped": "rule disabled"}


def test_decision_sub_scores_uncapped():
    recent = [_prior(m) for m in range(1, 50)]  # 49 prior within a 60-min window
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=50, avg_amount=100.0, total_amount=5000.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))
    config = _config()
    config["velocity"] = RuleConfig("velocity", weight=1.0, enabled=True,
                                     params={"window_minutes": 60, "threshold_count": 5})

    result = score_transaction(txn, recent, baseline, config)

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    assert velocity_result.sub_score == 10.0  # 50 / 5, uncapped, not clamped to 1.0


def test_decision_velocity_counts_current_transaction():
    recent = [_prior(1), _prior(2), _prior(3), _prior(4)]  # exactly 4 prior
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=4, avg_amount=100.0, total_amount=400.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))

    result = score_transaction(txn, recent, baseline, _config())

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    # 4 prior + 1 current = 5, threshold 5 -> fires; would NOT fire if only history counted
    assert velocity_result.fired is True
    assert velocity_result.details["count_in_window"] == 5


def test_edge_case_no_transaction_history_for_amount_baseline():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=1_000_000.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=0, avg_amount=0.0, total_amount=0.0,
                             fraud_count=0, last_transaction_at=None)

    result = score_transaction(txn, [], baseline, _config())

    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    assert amount_result.fired is False


def test_edge_case_no_prior_transaction_for_geo():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=LONDON[0], lon=LONDON[1])
    baseline = UserBaseline(transaction_count=0, avg_amount=0.0, total_amount=0.0,
                             fraud_count=0, last_transaction_at=None)

    result = score_transaction(txn, [], baseline, _config())

    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert geo_result.fired is False
