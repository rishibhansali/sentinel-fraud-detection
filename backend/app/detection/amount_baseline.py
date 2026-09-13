"""Amount-vs-baseline rule: flags deviation from the user's historical
average transaction amount, sourced from Phase 2's user_transaction_features
view. That view has avg_amount only (no standard deviation), so this
measures deviation as "multiples of the mean" rather than a proper
z-score — a confirmed, documented compromise, not an oversight.
"""
from .models import RuleConfig, RuleResult, Transaction, UserBaseline


def score_amount_baseline(
    transaction: Transaction,
    baseline: UserBaseline,
    config: RuleConfig,
) -> RuleResult:
    if not config.enabled:
        return RuleResult(
            rule_name=config.rule_name, fired=False, sub_score=0.0,
            details={"skipped": "rule disabled"},
        )

    if baseline.transaction_count == 0 or baseline.avg_amount == 0:
        return RuleResult(
            rule_name=config.rule_name,
            fired=False,
            sub_score=0.0,
            details={"reason": "no usable baseline (no history or zero average amount)"},
        )

    deviation_multiplier = config.params["deviation_multiplier"]
    relative_deviation = (transaction.amount - baseline.avg_amount) / baseline.avg_amount
    fired = abs(relative_deviation) >= deviation_multiplier
    sub_score = abs(relative_deviation) / deviation_multiplier

    return RuleResult(
        rule_name=config.rule_name,
        fired=fired,
        sub_score=sub_score,
        details={
            "amount": transaction.amount,
            "avg_amount": baseline.avg_amount,
            "relative_deviation": relative_deviation,
            "deviation_multiplier": deviation_multiplier,
        },
    )
