"""DB-facing loader that feeds the frozen, pure detection engine
(backend/app/detection/) with recent-transaction history. Lives outside
detection/ deliberately: detection/ never touches Postgres, this does.
"""
from datetime import datetime

from app.detection.models import Transaction, UserBaseline

# threshold_count for the velocity rule is 5 (infra/migrations/005_create_rules_config.sql).
# 20x that (100) is comfortably more than any legitimate velocity window could
# contain, while still bounding a single query against a pathologically high-volume
# user. Not a round number picked in the abstract.
DEFAULT_ROW_CAP = 100

_SELECT_RECENT_TRANSACTIONS = """
    SELECT id, user_id, ts, amount, lat, lon
    FROM transactions
    WHERE user_id = %(user_id)s
      AND (ts, id) < (%(before_ts)s, %(before_id)s)
    ORDER BY ts DESC, id DESC
    LIMIT %(row_cap)s;
"""


class UnsortedRecentTransactionsError(Exception):
    """Raised when a fetched recent-transactions list is not in strict
    descending-timestamp order. score_transaction()'s geo-impossibility rule
    reads recent_transactions[0] as the user's last-known location — silent
    misordering here would corrupt scoring without raising anywhere else.
    """


def _assert_sorted_desc(transactions: list[Transaction]) -> None:
    for i in range(len(transactions) - 1):
        if transactions[i].ts < transactions[i + 1].ts:
            raise UnsortedRecentTransactionsError(
                f"recent_transactions not sorted descending by ts: "
                f"index {i} (id={transactions[i].id}, ts={transactions[i].ts}) "
                f"precedes index {i + 1} (id={transactions[i + 1].id}, ts={transactions[i + 1].ts})"
            )


def get_recent_transactions(
    conn,
    user_id: int,
    before_ts: datetime,
    before_id: int,
    row_cap: int = DEFAULT_ROW_CAP,
) -> list[Transaction]:
    """Fetch a user's prior transactions, most-recent-first, for scoring.

    Takes a caller-owned `conn` rather than a dsn — deliberate divergence from
    detection/config.py's load_rules_config(), which opens/closes its own
    connection because it's called rarely (startup/refresh). This loader runs
    once per scored transaction under load, so the pipeline holds one
    connection/pool for the run and passes it in here.

    No time-window filtering happens in SQL. Sorting DESC means index 0 is
    always the most recent prior transaction whether or not it falls inside
    any rule's time window, so geo-impossibility (which only reads index 0)
    is always correctly served. Velocity's window is a rule-specific concern,
    not a loader concern — the caller splits it out in Python:

        recent = get_recent_transactions(conn, user_id, before_ts, before_id, row_cap)
        window_start = before_ts - timedelta(minutes=window_minutes)
        velocity_window = [t for t in recent if t.ts >= window_start]
        last_transaction = recent[0] if recent else None  # what geo_impossibility uses
    """
    with conn.cursor() as cur:
        cur.execute(
            _SELECT_RECENT_TRANSACTIONS,
            {
                "user_id": user_id,
                "before_ts": before_ts,
                "before_id": before_id,
                "row_cap": row_cap,
            },
        )
        rows = cur.fetchall()

    transactions = [
        Transaction(id=row[0], user_id=row[1], ts=row[2], amount=float(row[3]), lat=row[4], lon=row[5])
        for row in rows
    ]
    _assert_sorted_desc(transactions)
    return transactions


_SELECT_BASELINE = """
    SELECT transaction_count, avg_amount, total_amount, fraud_count, last_transaction_at
    FROM user_transaction_rollup
    WHERE user_id = %(user_id)s;
"""


def get_user_baseline(conn, user_id: int) -> UserBaseline:
    """Reads a user's aggregate baseline for amount_baseline scoring.

    Reads from `user_transaction_rollup` (materialized), not
    `user_transaction_features` (the live view) -- measured via EXPLAIN
    ANALYZE: the live view costs ~301ms per lookup (full aggregation over
    that user's transaction history) vs. ~0.04ms for the materialized
    rollup's indexed point lookup. At streaming/load-test volumes the live
    view is disqualifying; the rollup is the only real option for a
    once-per-scored-transaction hot path.

    That choice carries two separate, deliberate limitations -- neither
    addressed in this phase, both documented here at the same level of
    explicitness as Phase 3's z-score compromise (amount_baseline.py):

    1. Staleness from insertion timing: the rollup only reflects data as of
       its last `REFRESH MATERIALIZED VIEW CONCURRENTLY`. Nothing in this
       phase refreshes it automatically (Phase 2 left it the same way), so
       transactions inserted during a pipeline run are invisible to
       baseline lookups for the rest of that run.

    2. Look-ahead, not just staleness: the rollup is built once over the
       ENTIRE already-loaded ~3.1M-row dataset. Even freshly refreshed,
       transaction_count/avg_amount for a user reflect that user's FULL
       lifetime history -- including transactions chronologically AFTER
       the one currently being scored during replay. A transaction from
       early in the simulated stream is scored against a baseline that
       already contains everything that "happens" later in the replay. A
       real live system would only ever see a point-in-time baseline built
       from strictly-earlier transactions; this pipeline's baseline is not
       point-in-time. Building incremental/point-in-time baseline
       computation is explicitly out of scope for this phase -- this is a
       documented limitation, not a bug to fix here.

    On a miss (no rollup row for user_id -- e.g. their first-ever
    transaction, since the rollup only has rows for users who already have
    >=1 transaction), returns a zeroed UserBaseline rather than raising,
    matching the already-tested Phase 3 edge case in amount_baseline.py
    (baseline.transaction_count == 0 -> rule cannot fire).
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT_BASELINE, {"user_id": user_id})
        row = cur.fetchone()

    if row is None:
        return UserBaseline(transaction_count=0, avg_amount=0.0, total_amount=0.0,
                             fraud_count=0, last_transaction_at=None)

    return UserBaseline(
        transaction_count=row[0],
        avg_amount=float(row[1]),
        total_amount=float(row[2]),
        fraud_count=row[3],
        last_transaction_at=row[4],
    )
