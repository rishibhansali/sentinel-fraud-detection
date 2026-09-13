from datetime import datetime, timezone

from app.detection.models import RuleConfig, Transaction, UserBaseline
from app.detection.scoring import score_transaction

TS = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _all_enabled_config() -> dict[str, RuleConfig]:
    return {
        "velocity": RuleConfig("velocity", weight=1.0, enabled=True,
                                params={"window_minutes": 10, "threshold_count": 5}),
        "amount_baseline": RuleConfig("amount_baseline", weight=1.0, enabled=True,
                                       params={"deviation_multiplier": 3.0}),
        "geo_impossibility": RuleConfig("geo_impossibility", weight=1.0, enabled=True,
                                         params={"min_distance_km": 50.0, "max_speed_kmh": 900.0}),
    }


def test_score_transaction_no_rules_fire_when_nothing_anomalous():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=5, avg_amount=100.0, total_amount=500.0,
                             fraud_count=0, last_transaction_at=TS)

    result = score_transaction(txn, [], baseline, _all_enabled_config())

    assert len(result.rule_results) == 3
    assert all(r.fired is False for r in result.rule_results)
    # No rule fires, but velocity's sub_score is never exactly 0.0 even with
    # zero history: it counts the transaction being scored as part of its
    # own window (confirmed decision), so a single transaction always
    # contributes 1/threshold_count. "Nothing fired" does not mean
    # "total_score == 0.0" — that's expected, not a bug.
    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert velocity_result.sub_score == 0.2  # 1 transaction / threshold_count 5, self-inclusive
    expected_total = (velocity_result.sub_score + amount_result.sub_score + geo_result.sub_score) / 3
    assert abs(result.total_score - expected_total) < 1e-9


def test_score_transaction_zero_enabled_rules_guard():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=5, avg_amount=100.0, total_amount=500.0,
                             fraud_count=0, last_transaction_at=TS)
    config = {
        name: RuleConfig(name, weight=cfg.weight, enabled=False, params=cfg.params)
        for name, cfg in _all_enabled_config().items()
    }

    result = score_transaction(txn, [], baseline, config)

    assert result.total_score == 0.0
    assert len(result.rule_results) == 3
    assert all(r.fired is False for r in result.rule_results)
    assert all(r.details == {"skipped": "rule disabled"} for r in result.rule_results)


def test_score_transaction_disabled_rule_excluded_from_weighted_average():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=1000.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=5, avg_amount=100.0, total_amount=500.0,
                             fraud_count=0, last_transaction_at=TS)
    config = _all_enabled_config()
    config["velocity"] = RuleConfig("velocity", weight=1.0, enabled=False,
                                     params={"window_minutes": 10, "threshold_count": 5})

    result = score_transaction(txn, [], baseline, config)

    rule_names = {r.rule_name for r in result.rule_results}
    assert rule_names == {"velocity", "amount_baseline", "geo_impossibility"}

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    assert velocity_result.details == {"skipped": "rule disabled"}

    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    expected_total = (amount_result.sub_score + geo_result.sub_score) / 2  # velocity excluded, equal weights
    assert abs(result.total_score - expected_total) < 1e-9


def test_score_transaction_weighted_average_respects_unequal_weights():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=1000.0, lat=0.0, lon=0.0)  # triggers amount_baseline
    baseline = UserBaseline(transaction_count=5, avg_amount=100.0, total_amount=500.0,
                             fraud_count=0, last_transaction_at=TS)
    config = _all_enabled_config()
    config["amount_baseline"] = RuleConfig("amount_baseline", weight=3.0, enabled=True,
                                            params={"deviation_multiplier": 3.0})

    result = score_transaction(txn, [], baseline, config)

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    total_weight = 1.0 + 3.0 + 1.0
    expected_total = (
        1.0 * velocity_result.sub_score + 3.0 * amount_result.sub_score + 1.0 * geo_result.sub_score
    ) / total_weight
    assert abs(result.total_score - expected_total) < 1e-9
