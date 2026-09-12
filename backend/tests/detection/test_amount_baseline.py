from datetime import datetime, timezone

from app.detection.amount_baseline import score_amount_baseline
from app.detection.models import RuleConfig, Transaction, UserBaseline

TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _config(deviation_multiplier=3.0, enabled=True) -> RuleConfig:
    return RuleConfig(
        rule_name="amount_baseline", weight=1.0, enabled=enabled,
        params={"deviation_multiplier": deviation_multiplier},
    )


def _baseline(transaction_count=10, avg_amount=100.0) -> UserBaseline:
    return UserBaseline(
        transaction_count=transaction_count, avg_amount=avg_amount,
        total_amount=avg_amount * transaction_count, fraud_count=0,
        last_transaction_at=TS,
    )


def test_amount_baseline_fires_on_large_deviation():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=400.0, lat=0.0, lon=0.0)

    result = score_amount_baseline(txn, _baseline(avg_amount=100.0), _config(deviation_multiplier=3.0))

    # relative deviation = (400-100)/100 = 3.0, >= 3.0 -> fires
    assert result.fired is True
    assert result.details["relative_deviation"] == 3.0
    assert result.sub_score == 1.0


def test_amount_baseline_does_not_fire_within_normal_range():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=120.0, lat=0.0, lon=0.0)

    result = score_amount_baseline(txn, _baseline(avg_amount=100.0), _config(deviation_multiplier=3.0))

    assert result.fired is False


def test_amount_baseline_sub_score_uncapped():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=1000.0, lat=0.0, lon=0.0)

    result = score_amount_baseline(txn, _baseline(avg_amount=100.0), _config(deviation_multiplier=3.0))

    # relative deviation = 9.0, sub_score = 9.0 / 3.0 = 3.0, uncapped
    assert result.sub_score == 3.0


def test_amount_baseline_no_history_cannot_fire():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=1_000_000.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=0, avg_amount=0.0, total_amount=0.0,
                             fraud_count=0, last_transaction_at=None)

    result = score_amount_baseline(txn, baseline, _config())

    assert result.fired is False
    assert result.sub_score == 0.0
    assert "no usable baseline" in result.details["reason"]


def test_amount_baseline_zero_avg_amount_guarded():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=50.0, lat=0.0, lon=0.0)
    baseline = _baseline(transaction_count=3, avg_amount=0.0)

    result = score_amount_baseline(txn, baseline, _config())

    assert result.fired is False
    assert result.sub_score == 0.0
    assert "no usable baseline" in result.details["reason"]


def test_amount_baseline_disabled_rule_returns_skipped():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=50.0, lat=0.0, lon=0.0)

    result = score_amount_baseline(txn, _baseline(), _config(enabled=False))

    assert result.details == {"skipped": "rule disabled"}
