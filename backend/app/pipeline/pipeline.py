"""Wires ReplayHarness output through the frozen detection engine, persists
flagged cases, optionally annotates them after commit, and announces each
newly inserted case on Redis (`case.created`, after commit, best-effort).
Each case is stamped with the rules_config_version of the single
RulesProvider snapshot used to score it. Only rules decide case creation.
"""
import json
import logging
import math
from collections.abc import Sequence
from typing import Callable, Optional, Protocol, Union

import psycopg2.extras

from app.detection.models import RuleConfig, ScoreResult, Transaction
from app.detection.scoring import score_transaction
from app.pipeline.loader import DEFAULT_ROW_CAP, get_recent_transactions, get_user_baseline
from app.pipeline.priority import compute_priority, lookup_priority_inputs
from app.pipeline.rules_provider import RulesProvider
from app.realtime.events import case_summary
from app.realtime.publisher import publish_case_event

log = logging.getLogger(__name__)

_INSERT_FLAGGED_CASE = """
    INSERT INTO flagged_cases (transaction_id, transaction_ts, user_id, total_score, priority_score,
                               rule_results, rules_config_version, priority_adjustment)
    VALUES (%(transaction_id)s, %(transaction_ts)s, %(user_id)s, %(total_score)s, %(priority_score)s,
            %(rule_results)s, %(rules_config_version)s, %(priority_adjustment)s)
    ON CONFLICT (transaction_id) DO NOTHING
    RETURNING id;
"""

_SELECT_STORED = "SELECT total_score, rules_config_version FROM flagged_cases WHERE transaction_id = %s;"
_SELECT_CASE = "SELECT * FROM flagged_cases WHERE id = %s;"
_SELECT_ML_INPUTS = (
    "SELECT " + ", ".join(f"v{i}" for i in range(1, 29))
    + ", amount FROM transactions WHERE id = %s AND ts = %s;"
)
_UPDATE_ML_SCORE = "UPDATE flagged_cases SET ml_anomaly_score = %s WHERE id = %s;"
_UPDATE_SUMMARY = (
    "UPDATE flagged_cases SET ai_summary = %s, ai_summary_model = %s, "
    "ai_summary_generated_at = clock_timestamp() WHERE id = %s AND ai_summary IS NULL;"
)


class CaseSummarizer(Protocol):
    model: str

    def summarize(self, row: dict, transaction: Transaction) -> str: ...


def is_flagged(result: ScoreResult) -> bool:
    """A case is flagged when at least one rule concretely fired -- not by a
    total_score cutoff (confirmed decision). Every flagged row must be
    explainable by a specific fired rule, matching this project's core
    auditability principle. total_score is still stored on the row for
    Phase 5 to sort/prioritize by, it's just never the gate.
    """
    return any(r.fired for r in result.rule_results)


def persist_flagged_case(
    conn,
    transaction: Transaction,
    result: ScoreResult,
    rules_config_version: Optional[int] = None,
    priority_score: Optional[float] = None,
    priority_adjustment: Optional[dict] = None,
) -> Optional[int]:
    """Insert-if-absent. Returns the new case id, or None if a case for this
    transaction already existed (first write wins; the stored row is never
    altered). A skipped duplicate is logged with both the stored and the newly
    computed score/version. Does not commit. priority_score defaults to
    total_score and priority_adjustment to NULL (no demotion).
    """
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
            "rules_config_version": rules_config_version,
            "priority_score": result.total_score if priority_score is None else priority_score,
            "priority_adjustment": None if priority_adjustment is None else json.dumps(priority_adjustment),
        })
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute(_SELECT_STORED, (transaction.id,))
        stored = cur.fetchone()
    log.warning(
        "duplicate flagged case skipped for transaction %s: stored total_score=%s rules_config_version=%s, "
        "recomputed total_score=%s rules_config_version=%s",
        transaction.id,
        stored[0] if stored else None, stored[1] if stored else None,
        result.total_score, rules_config_version,
    )
    return None


def make_pipeline_callback(
    conn,
    rules: Union[RulesProvider, dict[str, RuleConfig]],
    row_cap: int = DEFAULT_ROW_CAP,
    anomaly_scorer: Callable[[Sequence[object], object], float] | None = None,
    case_summarizer: CaseSummarizer | None = None,
) -> Callable[[Transaction], None]:
    """Builds the callback passed to ReplayHarness.start(): loader -> baseline
    -> score_transaction() -> persist-if-flagged -> commit -> optional
    anomaly and summary annotations -> publish, all
    against a single caller-owned connection opened once by the driver.

    `rules` is a RulesProvider, or a plain dict (wrapped in a static provider
    with no version). provider.current() is read once per transaction so
    scoring and version stamping always use the same snapshot.

    This is a second, separate long-lived connection from ReplayHarness's own
    internal read connection. Two long-lived connections total, neither
    opened per-transaction.
    """
    provider = rules if isinstance(rules, RulesProvider) else RulesProvider.static(rules)

    def on_transaction(transaction: Transaction) -> None:
        snapshot = provider.current()
        recent = get_recent_transactions(conn, transaction.user_id, transaction.ts, transaction.id, row_cap)
        baseline = get_user_baseline(conn, transaction.user_id)
        result = score_transaction(transaction, recent, baseline, snapshot.rules_config)

        case_id = None
        if is_flagged(result):
            inputs = lookup_priority_inputs(
                conn, transaction.user_id, [r.rule_name for r in result.rule_results if r.fired]
            )
            priority_score, adjustment = compute_priority(result, snapshot.rules_config, inputs)
            case_id = persist_flagged_case(
                conn, transaction, result, snapshot.version,
                priority_score=priority_score, priority_adjustment=adjustment,
            )

        # Commit per transaction (not batched): keeps every flagged row
        # durable immediately. Candidate for batching if throughput numbers
        # show COMMIT round-trips are the bottleneck.
        conn.commit()

        if case_id is not None:
            if anomaly_scorer is not None:
                try:
                    with conn.cursor() as cur:
                        cur.execute(_SELECT_ML_INPUTS, (transaction.id, transaction.ts))
                        row = cur.fetchone()
                        if row is None:
                            raise ValueError(f"transaction {transaction.id} missing for anomaly annotation")
                        anomaly_score = float(anomaly_scorer(row[:28], row[28]))
                        if not math.isfinite(anomaly_score):
                            raise ValueError("anomaly scorer returned a nonfinite score")
                        cur.execute(_UPDATE_ML_SCORE, (anomaly_score, case_id))
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    log.warning(
                        "anomaly annotation failed for case %s transaction %s: %s",
                        case_id, transaction.id, exc,
                    )
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_SELECT_CASE, (case_id,))
                row = cur.fetchone()
            conn.commit()  # Never hold an open DB transaction during an API call.

            if row is not None and case_summarizer is not None:
                try:
                    summary = case_summarizer.summarize(row, transaction)
                    model = case_summarizer.model
                    if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 1000:
                        raise ValueError("case summarizer returned invalid text")
                    if not isinstance(model, str) or not model.strip():
                        raise ValueError("case summarizer returned an invalid model id")
                    with conn.cursor() as cur:
                        cur.execute(_UPDATE_SUMMARY, (summary.strip(), model, case_id))
                        if cur.rowcount != 1:
                            raise ValueError("case summary was not stored")
                    conn.commit()
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        cur.execute(_SELECT_CASE, (case_id,))
                        row = cur.fetchone()
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    log.warning(
                        "case summary failed for case %s transaction %s: %s",
                        case_id, transaction.id, exc,
                    )
            # Only after successful case commit, only for a newly inserted row.
            # publish_case_event never raises.
            if row is not None:
                publish_case_event("case.created", case_summary(row))

    return on_transaction
