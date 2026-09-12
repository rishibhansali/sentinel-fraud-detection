-- Composite index serving the naive per-user lookup query locked from
-- Phase 1: SELECT * FROM transactions WHERE user_id = ? ORDER BY ts DESC
-- LIMIT N. Built CONCURRENTLY so it doesn't hold a write lock while
-- building (matters in production; harmless here, but the right habit).
CREATE INDEX CONCURRENTLY idx_transactions_user_id_ts
    ON transactions (user_id, ts DESC);
