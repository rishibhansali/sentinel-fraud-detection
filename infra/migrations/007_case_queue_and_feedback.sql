\set ON_ERROR_STOP on
-- Phase 5 schema, one atomic migration (spec section 3). Order matters for
-- FKs: rules_config_history, then case_feedback, then the flagged_cases
-- alteration (which references both). Detection output columns of
-- flagged_cases stay immutable; analyst work lives in the new columns/tables.
-- ml_anomaly_score is created here (nullable, no default); Phase 7 fills it.
BEGIN;

-- 3.1 Append-only rules config history. id doubles as the config version.
CREATE TABLE rules_config_history (
    id BIGSERIAL PRIMARY KEY,
    rule_name TEXT NOT NULL,
    before JSONB NULL,
    after JSONB NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO rules_config_history (rule_name, before, after, changed_by)
SELECT rule_name,
       NULL,
       jsonb_build_object('weight', weight, 'enabled', enabled, 'params', params),
       'migration'
  FROM rules_config
 ORDER BY rule_name;

-- 3.2 Append-only analyst decision log. Claiming does not write here.
CREATE TABLE case_feedback (
    id BIGSERIAL PRIMARY KEY,
    case_id BIGINT NOT NULL REFERENCES flagged_cases(id),
    decision TEXT NOT NULL CHECK (decision IN ('confirmed_fraud', 'false_positive')),
    analyst TEXT NOT NULL,
    note TEXT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX case_feedback_case_decided_idx
    ON case_feedback (case_id, decided_at DESC, id DESC);

-- 3.3 flagged_cases becomes the case queue.
ALTER TABLE flagged_cases
    ADD COLUMN status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'in_review', 'confirmed_fraud', 'false_positive')),
    ADD COLUMN claimed_by TEXT NULL,
    ADD COLUMN claimed_at TIMESTAMPTZ NULL,
    ADD COLUMN ml_anomaly_score DOUBLE PRECISION NULL,
    ADD COLUMN rules_config_version BIGINT NULL REFERENCES rules_config_history(id),
    ADD COLUMN priority_score DOUBLE PRECISION NULL,
    ADD COLUMN priority_adjustment JSONB NULL;

UPDATE flagged_cases SET priority_score = total_score;
ALTER TABLE flagged_cases ALTER COLUMN priority_score SET NOT NULL;

ALTER TABLE flagged_cases
    ADD CONSTRAINT flagged_cases_claim_pair_chk
        CHECK ((claimed_by IS NULL) = (claimed_at IS NULL)),
    ADD CONSTRAINT flagged_cases_in_review_has_claimant_chk
        CHECK (status <> 'in_review' OR claimed_by IS NOT NULL),
    ADD CONSTRAINT flagged_cases_open_unclaimed_chk
        CHECK (status <> 'open' OR claimed_by IS NULL);

CREATE INDEX flagged_cases_queue_idx
    ON flagged_cases (status, priority_score DESC, id DESC);
CREATE INDEX flagged_cases_user_false_positive_idx
    ON flagged_cases (user_id) WHERE status = 'false_positive';
CREATE INDEX flagged_cases_user_confirmed_fraud_idx
    ON flagged_cases (user_id) WHERE status = 'confirmed_fraud';

COMMIT;
