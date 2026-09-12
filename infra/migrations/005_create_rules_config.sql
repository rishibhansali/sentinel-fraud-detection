\set ON_ERROR_STOP on
-- One row per detection rule, not a single blob, so Phase 5's API and
-- Phase 9's UI have a natural per-rule edit path. No write/edit path is
-- built in this phase — only the read side (load_rules_config()) exists.
BEGIN;

CREATE TABLE rules_config (
    rule_name TEXT PRIMARY KEY,
    weight DOUBLE PRECISION NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    params JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO rules_config (rule_name, weight, params) VALUES
    ('velocity', 1.0, '{"window_minutes": 10, "threshold_count": 5}'),
    ('amount_baseline', 1.0, '{"deviation_multiplier": 3.0}'),
    ('geo_impossibility', 1.0, '{"min_distance_km": 50.0, "max_speed_kmh": 900.0}');

COMMIT;
