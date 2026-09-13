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


def test_decision_geo_min_distance_guard_through_combiner():
    # Previous and current are ~16.9km apart (well under the 50km guard) one
    # minute apart. Naive speed (16.9km / (1/60)h ~= 1011 km/h) would exceed
    # max_speed_kmh (900.0) and look "impossible" at a glance -- proving the
    # guard, not merely a low speed, is what suppresses the fire. Velocity
    # and amount_baseline are simultaneously in play (non-zero, non-firing)
    # so total_score is a genuine weighted-average check, not a 0.0 no-op.
    #
    # Hand-derivation:
    #   velocity: recent=[previous] (ts = TS-1min, inside the 10-min window)
    #     -> count_in_window = 1 (prior) + 1 (current) = 2; sub_score = 2/5 = 0.4; not fired.
    #   amount_baseline: amount 250.0 vs avg_amount 100.0
    #     -> relative_deviation = (250-100)/100 = 1.5; sub_score = 1.5/3.0 = 0.5; not fired (1.5 < 3.0).
    #   geo_impossibility: distance ~16.9km < min_distance_km 50.0 -> guard blocks -> sub_score 0.0; not fired.
    #   total_score = (0.4 + 0.5 + 0.0) / 3 = 0.9 / 3 = 0.3
    previous = Transaction(id=1, user_id=1, ts=TS - timedelta(minutes=1), amount=100.0,
                            lat=NYC[0], lon=NYC[1])
    txn = Transaction(id=99, user_id=1, ts=TS, amount=250.0, lat=NYC[0], lon=NYC[1] + 0.2)
    baseline = UserBaseline(transaction_count=1, avg_amount=100.0, total_amount=100.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))

    result = score_transaction(txn, [previous], baseline, _config())

    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert geo_result.fired is False
    assert geo_result.details["reason"] == "distance below minimum guard"

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    assert velocity_result.fired is False
    assert velocity_result.sub_score == 0.4
    assert amount_result.fired is False
    assert amount_result.sub_score == 0.5

    # total_score reflects only velocity's and amount_baseline's contributions;
    # geo contributes 0.0 because the guard suppressed it, not because it was skipped.
    assert result.total_score == 0.3


def test_decision_geo_zero_elapsed_time_automatic_fire():
    # Geo's zero-elapsed-time automatic fire, with amount_baseline simultaneously
    # firing (amount deviates 4x from baseline), proving the automatic-fire path
    # is unaffected by another rule also being anomalous in the same scoring call.
    #
    # Hand-derivation:
    #   velocity: recent=[previous] (ts = TS, inside the 10-min window)
    #     -> count_in_window = 1 + 1 = 2; sub_score = 2/5 = 0.4; not fired.
    #   amount_baseline: amount 500.0 vs avg_amount 100.0
    #     -> relative_deviation = (500-100)/100 = 4.0; sub_score = 4.0/3.0 = 1.3333...; fired (4.0 >= 3.0).
    #   geo_impossibility: same ts as previous -> automatic fire;
    #     distance NYC<->LONDON = 5570.222179737958km; sub_score = 5570.222179737958/50.0 = 111.40444359475916.
    #   total_score = (0.4 + 1.3333333333333333 + 111.40444359475916) / 3 = 37.71259230936416
    previous = Transaction(id=1, user_id=1, ts=TS, amount=100.0, lat=NYC[0], lon=NYC[1])
    txn = Transaction(id=99, user_id=1, ts=TS, amount=500.0, lat=LONDON[0], lon=LONDON[1])
    baseline = UserBaseline(transaction_count=1, avg_amount=100.0, total_amount=100.0,
                             fraud_count=0, last_transaction_at=TS)

    result = score_transaction(txn, [previous], baseline, _config())

    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert geo_result.fired is True
    assert geo_result.details["elapsed_hours"] == 0.0

    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    assert amount_result.fired is True
    assert amount_result.sub_score == 4.0 / 3.0

    assert result.total_score == 37.71259230936416


def test_decision_disabled_rule_never_omitted():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=1, avg_amount=100.0, total_amount=100.0,
                             fraud_count=0, last_transaction_at=TS)

    result = score_transaction(txn, [], baseline, _config(velocity_enabled=False))

    assert len(result.rule_results) == 3
    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    assert velocity_result.details == {"skipped": "rule disabled"}


def test_decision_sub_scores_uncapped():
    # Velocity's uncapped sub_score, alongside amount_baseline also firing
    # (amount is 5x baseline), proving the uncapped-sub-score behavior holds
    # even while another rule simultaneously contributes to the weighted average.
    #
    # Hand-derivation:
    #   velocity: 49 prior (all lat=0/lon=0, within the 60-min window) + current = 50
    #     -> sub_score = 50/5 = 10.0, uncapped; fired (50 >= 5).
    #   amount_baseline: amount 500.0 vs avg_amount 100.0
    #     -> relative_deviation = (500-100)/100 = 4.0; sub_score = 4.0/3.0 = 1.3333...; fired (4.0 >= 3.0).
    #   geo_impossibility: current at (0.0, 0.0), previous (recent[0], 1 min ago) also at (0.0, 0.0)
    #     -> distance 0.0km < min_distance_km 50.0 -> guard blocks -> sub_score 0.0; not fired.
    #   total_score = (10.0 + 1.3333333333333333 + 0.0) / 3 = 3.777777777777778
    recent = [_prior(m) for m in range(1, 50)]  # 49 prior within a 60-min window
    txn = Transaction(id=99, user_id=1, ts=TS, amount=500.0, lat=0.0, lon=0.0)  # 5x avg_amount
    baseline = UserBaseline(transaction_count=50, avg_amount=100.0, total_amount=5000.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))
    config = _config()
    config["velocity"] = RuleConfig("velocity", weight=1.0, enabled=True,
                                     params={"window_minutes": 60, "threshold_count": 5})

    result = score_transaction(txn, recent, baseline, config)

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    assert velocity_result.fired is True
    assert velocity_result.sub_score == 10.0  # 50 / 5, uncapped, not clamped to 1.0

    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    assert amount_result.fired is True
    assert amount_result.sub_score == 4.0 / 3.0

    assert result.total_score == 3.777777777777778


def test_decision_velocity_counts_current_transaction():
    # Velocity's self-inclusive count (4 prior + current = 5 -> fires), alongside
    # geo_impossibility also firing (recent[0] is in NYC, current is in London),
    # proving the self-counting behavior holds even while another rule fires too.
    #
    # Hand-derivation:
    #   velocity: 4 prior (1,2,3,4 min ago) + current = 5 -> sub_score = 5/5 = 1.0; fired (5 >= 5).
    #   amount_baseline: amount 100.0 vs avg_amount 100.0 -> relative_deviation = 0.0; sub_score 0.0; not fired.
    #   geo_impossibility: previous (recent[0], 1 min ago) in NYC, current in London
    #     -> distance = 5570.222179737958km, elapsed_hours = 1/60 = 0.016666666666666666
    #     -> implied_speed_kmh = 5570.222179737958 / 0.016666666666666666 = 334213.33078427747
    #     -> sub_score = 334213.33078427747 / 900.0 = 371.34814531586386; fired (>> 900.0).
    #   total_score = (1.0 + 0.0 + 371.34814531586386) / 3 = 124.11604843862129
    recent = [
        Transaction(id=1, user_id=1, ts=TS - timedelta(minutes=1), amount=100.0, lat=NYC[0], lon=NYC[1]),
        _prior(2), _prior(3), _prior(4),
    ]  # exactly 4 prior
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=LONDON[0], lon=LONDON[1])
    baseline = UserBaseline(transaction_count=4, avg_amount=100.0, total_amount=400.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))

    result = score_transaction(txn, recent, baseline, _config())

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    # 4 prior + 1 current = 5, threshold 5 -> fires; would NOT fire if only history counted
    assert velocity_result.fired is True
    assert velocity_result.details["count_in_window"] == 5

    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert geo_result.fired is True
    assert geo_result.sub_score == 371.34814531586386

    assert result.total_score == 124.11604843862129


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
