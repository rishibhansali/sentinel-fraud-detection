-- Reusable aggregation logic as a plain view — this is the single
-- source of truth for "per-user transaction stats", intended to be
-- referenced directly by Phase 6's feature engineering later rather
-- than re-derived inline in application code.
CREATE VIEW user_transaction_features AS
SELECT
    user_id,
    count(*) AS transaction_count,
    avg(amount) AS avg_amount,
    sum(amount) AS total_amount,
    sum(class) AS fraud_count,
    max(ts) AS last_transaction_at
FROM transactions
GROUP BY user_id;

-- Materialized wrapper: caches the same query so reads don't re-scan
-- the full transactions table on every request. Refreshed on a
-- schedule (proposed: every 5 minutes — see BENCHMARK.md), not
-- real-time incremental. No scheduler is implemented in this phase;
-- refresh manually via:
--   REFRESH MATERIALIZED VIEW CONCURRENTLY user_transaction_rollup;
CREATE MATERIALIZED VIEW user_transaction_rollup AS
SELECT * FROM user_transaction_features;

-- Required for REFRESH ... CONCURRENTLY (non-blocking refresh, so
-- reads against the view are never blocked by a refresh in progress).
CREATE UNIQUE INDEX idx_user_transaction_rollup_user_id
    ON user_transaction_rollup (user_id);
