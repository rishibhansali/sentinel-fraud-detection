from datetime import datetime, timedelta, timezone

from app.detection.geo_impossibility import score_geo_impossibility
from app.detection.models import RuleConfig, Transaction

TS = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)


def _config(min_distance_km=50.0, max_speed_kmh=900.0, enabled=True) -> RuleConfig:
    return RuleConfig(
        rule_name="geo_impossibility", weight=1.0, enabled=enabled,
        params={"min_distance_km": min_distance_km, "max_speed_kmh": max_speed_kmh},
    )


def test_geo_fires_on_impossible_speed():
    previous = Transaction(id=1, user_id=1, ts=TS, amount=10.0, lat=NYC[0], lon=NYC[1])
    current = Transaction(id=2, user_id=1, ts=TS + timedelta(minutes=30), amount=10.0, lat=LONDON[0], lon=LONDON[1])

    result = score_geo_impossibility(current, [previous], _config())

    assert result.fired is True
    assert result.details["implied_speed_kmh"] > 900.0


def test_geo_does_not_fire_within_min_distance_guard():
    previous = Transaction(id=1, user_id=1, ts=TS, amount=10.0, lat=40.0, lon=-74.0)
    current = Transaction(id=2, user_id=1, ts=TS + timedelta(minutes=1), amount=10.0, lat=40.001, lon=-74.001)

    result = score_geo_impossibility(current, [previous], _config())

    assert result.fired is False
    assert result.details["reason"] == "distance below minimum guard"


def test_geo_zero_elapsed_time_automatic_fire():
    previous = Transaction(id=1, user_id=1, ts=TS, amount=10.0, lat=NYC[0], lon=NYC[1])
    current = Transaction(id=2, user_id=1, ts=TS, amount=10.0, lat=LONDON[0], lon=LONDON[1])  # same ts

    result = score_geo_impossibility(current, [previous], _config())

    assert result.fired is True
    assert result.details["elapsed_hours"] == 0.0
    assert "identical timestamps" in result.details["reason"]
    assert result.sub_score > 1.0


def test_geo_no_prior_transaction_cannot_fire():
    current = Transaction(id=2, user_id=1, ts=TS, amount=10.0, lat=LONDON[0], lon=LONDON[1])

    result = score_geo_impossibility(current, [], _config())

    assert result.fired is False
    assert result.sub_score == 0.0
    assert "no prior transaction" in result.details["reason"]


def test_geo_disabled_rule_returns_skipped():
    current = Transaction(id=2, user_id=1, ts=TS, amount=10.0, lat=LONDON[0], lon=LONDON[1])

    result = score_geo_impossibility(current, [], _config(enabled=False))

    assert result.details == {"skipped": "rule disabled"}
