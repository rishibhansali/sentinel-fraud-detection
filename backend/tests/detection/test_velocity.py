from datetime import datetime, timedelta, timezone

from app.detection.models import RuleConfig, Transaction
from app.detection.velocity import score_velocity

BASE_TS = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _txn(minutes_ago: int, id_: int = 1) -> Transaction:
    return Transaction(
        id=id_, user_id=1, ts=BASE_TS - timedelta(minutes=minutes_ago),
        amount=10.0, lat=0.0, lon=0.0,
    )


def _current() -> Transaction:
    return Transaction(id=99, user_id=1, ts=BASE_TS, amount=10.0, lat=0.0, lon=0.0)


def _config(window_minutes=10, threshold_count=5, enabled=True) -> RuleConfig:
    return RuleConfig(
        rule_name="velocity", weight=1.0, enabled=enabled,
        params={"window_minutes": window_minutes, "threshold_count": threshold_count},
    )


def test_velocity_fires_when_count_including_current_meets_threshold():
    recent = [_txn(1), _txn(2), _txn(3), _txn(4)]  # 4 prior, all within 10 min

    result = score_velocity(_current(), recent, _config())

    assert result.fired is True
    assert result.details["count_in_window"] == 5
    assert result.sub_score == 1.0


def test_velocity_does_not_fire_below_threshold():
    recent = [_txn(1), _txn(2), _txn(3)]  # 3 prior -> 4 total, threshold 5

    result = score_velocity(_current(), recent, _config())

    assert result.fired is False
    assert result.details["count_in_window"] == 4


def test_velocity_excludes_transactions_outside_window():
    recent = [_txn(1), _txn(2), _txn(30)]  # 30-min-old one is outside the 10-min window

    result = score_velocity(_current(), recent, _config(threshold_count=3))

    assert result.details["count_in_window"] == 3  # 2 in-window + current
    assert result.fired is True


def test_velocity_sub_score_uncapped_above_threshold():
    recent = [_txn(m) for m in range(1, 50)]  # 49 prior, all within a 60-min window

    result = score_velocity(_current(), recent, _config(window_minutes=60, threshold_count=5))

    assert result.details["count_in_window"] == 50
    assert result.sub_score == 10.0  # 50 / 5, uncapped
    assert result.sub_score > 1.0


def test_velocity_disabled_rule_returns_skipped():
    result = score_velocity(_current(), [], _config(enabled=False))

    assert result.fired is False
    assert result.sub_score == 0.0
    assert result.details == {"skipped": "rule disabled"}
