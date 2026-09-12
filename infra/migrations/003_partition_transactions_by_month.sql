\set ON_ERROR_STOP on
-- Rebuild `transactions` as a monthly range-partitioned table on `ts`.
-- Postgres has no in-place ALTER TABLE ... PARTITION BY; this requires a
-- rename + rebuild + copy. Primary key becomes (id, ts) because Postgres
-- requires the partition key to be part of any unique constraint — a
-- no-op in practice (id was already globally unique on its own) but a
-- real, confirmed schema change.
--
-- Known characteristic: Phase 1's synthetic timestamps span only ~23
-- days (Jan 1-23, 2025), so nearly all 3.1M rows land in the single
-- transactions_2025_01 partition. This does not affect the correctness
-- of this migration; it does mean partitioning prunes nothing for the
-- Phase 1/2 benchmark query even in principle (see BENCHMARK.md).

ALTER TABLE transactions RENAME TO transactions_old;

-- ALTER TABLE ... RENAME TO does NOT rename the table's indexes or
-- constraints -- they stay bound to their original names on the renamed
-- table. Rename them out of the way first so the new `transactions` table
-- created below can claim the canonical `idx_transactions_user_id_ts`,
-- `transactions_pkey`, and `transactions_split_check` names on the first
-- try, instead of erroring (index) or silently getting Postgres's
-- auto-suffixed fallback names like `transactions_pkey1` (constraints).
ALTER INDEX idx_transactions_user_id_ts RENAME TO idx_transactions_user_id_ts_old;
ALTER TABLE transactions_old RENAME CONSTRAINT transactions_pkey TO transactions_old_pkey;
ALTER TABLE transactions_old RENAME CONSTRAINT transactions_split_check TO transactions_old_split_check;

CREATE TABLE transactions (
    id BIGINT NOT NULL,
    user_id INTEGER NOT NULL,
    card_id INTEGER NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    amount NUMERIC(12, 2) NOT NULL,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    v1 DOUBLE PRECISION NOT NULL,
    v2 DOUBLE PRECISION NOT NULL,
    v3 DOUBLE PRECISION NOT NULL,
    v4 DOUBLE PRECISION NOT NULL,
    v5 DOUBLE PRECISION NOT NULL,
    v6 DOUBLE PRECISION NOT NULL,
    v7 DOUBLE PRECISION NOT NULL,
    v8 DOUBLE PRECISION NOT NULL,
    v9 DOUBLE PRECISION NOT NULL,
    v10 DOUBLE PRECISION NOT NULL,
    v11 DOUBLE PRECISION NOT NULL,
    v12 DOUBLE PRECISION NOT NULL,
    v13 DOUBLE PRECISION NOT NULL,
    v14 DOUBLE PRECISION NOT NULL,
    v15 DOUBLE PRECISION NOT NULL,
    v16 DOUBLE PRECISION NOT NULL,
    v17 DOUBLE PRECISION NOT NULL,
    v18 DOUBLE PRECISION NOT NULL,
    v19 DOUBLE PRECISION NOT NULL,
    v20 DOUBLE PRECISION NOT NULL,
    v21 DOUBLE PRECISION NOT NULL,
    v22 DOUBLE PRECISION NOT NULL,
    v23 DOUBLE PRECISION NOT NULL,
    v24 DOUBLE PRECISION NOT NULL,
    v25 DOUBLE PRECISION NOT NULL,
    v26 DOUBLE PRECISION NOT NULL,
    v27 DOUBLE PRECISION NOT NULL,
    v28 DOUBLE PRECISION NOT NULL,
    class SMALLINT NOT NULL,
    split TEXT NOT NULL CHECK (split IN ('train', 'val', 'test')),
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE transactions_2024_12 PARTITION OF transactions
    FOR VALUES FROM ('2024-12-01') TO ('2025-01-01');
CREATE TABLE transactions_2025_01 PARTITION OF transactions
    FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');
CREATE TABLE transactions_2025_02 PARTITION OF transactions
    FOR VALUES FROM ('2025-02-01') TO ('2025-03-01');
CREATE TABLE transactions_2025_03 PARTITION OF transactions
    FOR VALUES FROM ('2025-03-01') TO ('2025-04-01');
CREATE TABLE transactions_default PARTITION OF transactions DEFAULT;

-- Declaring the index on the partitioned parent propagates it to every
-- partition automatically (Postgres creates one physical index per
-- partition, tied together as a single logical "partitioned index").
CREATE INDEX idx_transactions_user_id_ts ON transactions (user_id, ts DESC);

INSERT INTO transactions SELECT * FROM transactions_old;
