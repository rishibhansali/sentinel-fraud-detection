"""Velocity rule: fires when too many transactions land in a short window.
Counts the transaction being scored as one of its own window (confirmed
decision) — threshold_count=5 means 5 transactions total in the window,
including the current one, not 5 prior transactions plus the current.
"""
from datetime import timedelta

from .models import RuleConfig, RuleResult, Transaction


def score_velocity(
    transaction: Transaction,
    recent_transactions: list[Transaction],
    config: RuleConfig,
) -> RuleResult:
    if not config.enabled:
        return RuleResult(
            rule_name=config.rule_name, fired=False, sub_score=0.0,
            details={"skipped": "rule disabled"},
        )

    window_minutes = config.params["window_minutes"]
    threshold_count = config.params["threshold_count"]
    window_start = transaction.ts - timedelta(minutes=window_minutes)

    matching = [t for t in recent_transactions if window_start <= t.ts <= transaction.ts]
    count_including_current = len(matching) + 1
    fired = count_including_current >= threshold_count
    sub_score = count_including_current / threshold_count

    return RuleResult(
        rule_name=config.rule_name,
        fired=fired,
        sub_score=sub_score,
        details={
            "count_in_window": count_including_current,
            "window_minutes": window_minutes,
            "threshold_count": threshold_count,
        },
    )
