-- Optional Phase 8 explanation of a case that rules have already created.
ALTER TABLE flagged_cases
    ADD COLUMN ai_summary TEXT NULL,
    ADD COLUMN ai_summary_model TEXT NULL,
    ADD COLUMN ai_summary_generated_at TIMESTAMPTZ NULL,
    ADD CONSTRAINT flagged_cases_ai_summary_complete CHECK (
        (ai_summary IS NULL AND ai_summary_model IS NULL AND ai_summary_generated_at IS NULL)
        OR
        (ai_summary IS NOT NULL AND ai_summary_model IS NOT NULL AND ai_summary_generated_at IS NOT NULL)
    );
