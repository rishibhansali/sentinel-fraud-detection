\set ON_ERROR_STOP on
-- Minimal persistence for Phase 4's live pipeline: a transaction that clears
-- the flag threshold (any rule fired -- see backend/app/pipeline/pipeline.py)
-- gets one row here with its score and full rule breakdown. NOT the case
-- queue schema -- that's Phase 5. This exists so Phase 5 has something real
-- to build a queue API on top of without redoing this persistence step.
--
-- No FOREIGN KEY to transactions(id): transactions is partitioned with
-- primary key (id, ts), which would force a composite FK carrying ts too.
-- The UNIQUE constraint on transaction_id gives the practical guarantee this
-- table actually needs (a transaction can't be flagged twice) without that
-- complexity.
BEGIN;

CREATE TABLE flagged_cases (
    id BIGSERIAL PRIMARY KEY,
    transaction_id BIGINT NOT NULL UNIQUE,
    transaction_ts TIMESTAMPTZ NOT NULL,
    user_id INTEGER NOT NULL,
    total_score DOUBLE PRECISION NOT NULL,
    rule_results JSONB NOT NULL,
    flagged_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
