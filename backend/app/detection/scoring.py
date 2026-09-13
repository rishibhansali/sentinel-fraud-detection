"""Combines the three rule sub-scores into a single weighted, normalized
total score. Zero-enabled-rules is guarded explicitly (confirmed
decision) rather than left to a 0/0 division.
"""
from .amount_baseline import score_amount_baseline
from .geo_impossibility import score_geo_impossibility
from .models import RuleConfig, ScoreResult, Transaction, UserBaseline
from .velocity import score_velocity


def score_transaction(
    transaction: Transaction,
    recent_transactions: list[Transaction],
    baseline: UserBaseline,
    rules_config: dict[str, RuleConfig],
) -> ScoreResult:
    rule_results = [
        score_velocity(transaction, recent_transactions, rules_config["velocity"]),
        score_amount_baseline(transaction, baseline, rules_config["amount_baseline"]),
        score_geo_impossibility(transaction, recent_transactions, rules_config["geo_impossibility"]),
    ]

    enabled_results = [r for r in rule_results if rules_config[r.rule_name].enabled]
    enabled_weight = sum(rules_config[r.rule_name].weight for r in enabled_results)

    if enabled_weight == 0:
        total_score = 0.0
    else:
        total_score = sum(
            rules_config[r.rule_name].weight * r.sub_score for r in enabled_results
        ) / enabled_weight

    return ScoreResult(total_score=total_score, rule_results=rule_results)
