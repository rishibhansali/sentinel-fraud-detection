"""Integration test for load_rules_config() against the real running
Postgres instance (matching this project's established testing pattern
from Phases 1-2) — not mocked.
"""
import os

from app.detection.config import load_rules_config

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)


def test_load_rules_config_returns_all_seeded_rules():
    config = load_rules_config(DB_DSN)

    assert set(config.keys()) == {"velocity", "amount_baseline", "geo_impossibility"}
    assert config["velocity"].enabled is True
    assert config["velocity"].weight == 1.0
    assert config["velocity"].params["window_minutes"] == 10
    assert config["velocity"].params["threshold_count"] == 5
    assert config["amount_baseline"].params["deviation_multiplier"] == 3.0
    assert config["geo_impossibility"].params["min_distance_km"] == 50.0
    assert config["geo_impossibility"].params["max_speed_kmh"] == 900.0
