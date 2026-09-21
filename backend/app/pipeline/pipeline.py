"""Wires ReplayHarness output through the frozen detection engine and
persists flagged cases. Deliberately ends here: no WebSocket/UI delivery, no
ML score, no Claude summary -- this phase ends at flagged rows existing in
Postgres with their score and rule breakdown. Phase 5 owns the case queue;
Phase 7/8 own ML and Claude.
"""
import json
from typing import Callable

from app.detection.models import RuleConfig, ScoreResult, Transaction
from app.detection.scoring import score_transaction
from app.pipeline.loader import DEFAULT_ROW_CAP, get_recent_transactions, get_user_baseline

_INSERT_FLAGGED_CASE = """
    INSERT INTO flagged_cases (transaction_id, transaction_ts, user_id, total_score, priority_score, rule_results)
    VALUES (%(transaction_id)s, %(transaction_ts)s, %(user_id)s, %(total_score)s, %(total_score)s, %(rule_results)s)
    ON CONFLICT (transaction_id) DO NOTHING;
"""


def is_flagged(result: ScoreResult) -> bool:
    """A case is flagged when at least one rule concretely fired -- not by a
    total_score cutoff (confirmed decision). Every flagged row must be
    explainable by a specific fired rule, matching this project's core
    auditability principle. total_score is still stored on the row for
    Phase 5 to sort/prioritize by, it's just never the gate.
    """
    return any(r.fired for r in result.rule_results)


def persist_flagged_case(conn, transaction: Transaction, result: ScoreResult) -> None:
    rule_results_json = json.dumps([
        {"rule_name": r.rule_name, "fired": r.fired, "sub_score": r.sub_score, "details": r.details}
        for r in result.rule_results
    ])
    with conn.cursor() as cur:
        cur.execute(_INSERT_FLAGGED_CASE, {
            "transaction_id": transaction.id,
            "transaction_ts": transaction.ts,
            "user_id": transaction.user_id,
            "total_score": result.total_score,
            "rule_results": rule_results_json,
        })


def make_pipeline_callback(
    conn,
    rules_config: dict[str, RuleConfig],
    row_cap: int = DEFAULT_ROW_CAP,
) -> Callable[[Transaction], None]:
    """Builds the callback passed to ReplayHarness.start(): loader -> baseline
    -> score_transaction() -> persist-if-flagged, all against a single
    caller-owned connection opened once by the driver, not per transaction.

    This is a second, separate long-lived connection from ReplayHarness's own
    internal read connection (used only for reading replay-source batches) --
    not shared with it, since ReplayHarness's callback interface is already
    closed/reviewed and isn't reopened here. Two long-lived connections
    total, neither opened per-transaction.
    """
    def on_transaction(transaction: Transaction) -> None:
        recent = get_recent_transactions(conn, transaction.user_id, transaction.ts, transaction.id, row_cap)
        baseline = get_user_baseline(conn, transaction.user_id)
        result = score_transaction(transaction, recent, baseline, rules_config)

        if is_flagged(result):
            persist_flagged_case(conn, transaction, result)

        # Commit per transaction (not batched): keeps every flagged row
        # durable immediately and avoids one long-running session transaction
        # across the whole replay. Candidate for batching (e.g. every N
        # transactions, or only after an actual insert) if Task 4's
        # throughput numbers show COMMIT round-trips -- not scoring logic --
        # are the bottleneck. Not done now: no throughput number exists yet
        # to justify it.
        conn.commit()

    return on_transaction
