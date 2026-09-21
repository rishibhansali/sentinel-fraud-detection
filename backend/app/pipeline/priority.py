"""Priority demotion (spec section 8, formula_version 1).

Analyst feedback affects how future flags are PRIORITIZED, never whether they
are created. A false-positive mark can only lower priority within a bounded
range (priority_score >= FLOOR * total_score) and any confirmed_fraud for the
user removes all demotion for that user. total_score is never touched.

The lookup uses each case's CURRENT status, so correcting a case from
false_positive to confirmed_fraud removes its influence (and vetoes).
"""
import json
from dataclasses import dataclass, field
from typing import Optional

from app.detection.models import RuleConfig, ScoreResult

FORMULA_VERSION = 1
DECAY = 0.8
FLOOR = 0.5
CITED_IDS_MAX = 10

_FP_LOOKUP = """
    SELECT count(*), (array_agg(id ORDER BY id DESC))[1:%(max_ids)s]
      FROM flagged_cases
     WHERE user_id = %(user_id)s
       AND status = 'false_positive'
       AND rule_results @> %(needle)s::jsonb;
"""
_VETO_LOOKUP = """
    SELECT EXISTS (SELECT 1 FROM flagged_cases WHERE user_id = %s AND status = 'confirmed_fraud');
"""


@dataclass(frozen=True)
class PriorityInputs:
    veto: bool = False
    # rule_name -> (n_r, most recent <= CITED_IDS_MAX case ids, newest first)
    false_positives: dict = field(default_factory=dict)


def lookup_priority_inputs(conn, user_id: int, fired_rules) -> PriorityInputs:
    """Caller-owned connection; does not commit. Call only for flagged
    transactions. Only rules that fired are looked up."""
    fired_rules = list(fired_rules)
    if not fired_rules:
        return PriorityInputs()
    with conn.cursor() as cur:
        cur.execute(_VETO_LOOKUP, (user_id,))
        veto = bool(cur.fetchone()[0])
        if veto:  # any confirmed_fraud removes all demotion; skip the rest
            return PriorityInputs(veto=True)
        fps = {}
        for name in fired_rules:
            cur.execute(_FP_LOOKUP, {
                "user_id": user_id,
                "max_ids": CITED_IDS_MAX,
                "needle": json.dumps([{"rule_name": name, "fired": True}]),
            })
            count, ids = cur.fetchone()
            if count:
                fps[name] = (count, list(ids or []))
    return PriorityInputs(veto=False, false_positives=fps)


def combine(sub_scores: dict[str, float], rules_config: dict[str, RuleConfig]) -> float:
    """Weighted average over ENABLED rules. Deliberate local duplicate of the
    combiner in detection.scoring.score_transaction (frozen engine, not
    edited); test_priority.py asserts exact parity."""
    enabled = [n for n in sub_scores if rules_config[n].enabled]
    enabled_weight = sum(rules_config[n].weight for n in enabled)
    if enabled_weight == 0:
        return 0.0
    return sum(rules_config[n].weight * sub_scores[n] for n in enabled) / enabled_weight


def demotion_factor(n: int) -> float:
    return max(FLOOR, DECAY ** n)


def compute_priority(
    result: ScoreResult,
    rules_config: dict[str, RuleConfig],
    inputs: PriorityInputs,
) -> tuple[float, Optional[dict]]:
    """Pure. Returns (priority_score, priority_adjustment); the adjustment is
    None when nothing was adjusted (then priority_score == total_score)."""
    per_rule = []
    subs: dict[str, float] = {}
    for r in result.rule_results:
        sub = r.sub_score
        n, ids = inputs.false_positives.get(r.rule_name, (0, []))
        enabled = rules_config[r.rule_name].enabled
        if r.fired and enabled and not inputs.veto and n > 0:
            factor = demotion_factor(n)
            sub = r.sub_score * factor
            per_rule.append({
                "rule_name": r.rule_name,
                "prior_false_positive_count": n,
                "prior_false_positive_case_ids": sorted(ids[:CITED_IDS_MAX]),
                "factor": factor,
            })
        subs[r.rule_name] = sub
    score = combine(subs, rules_config)
    if not per_rule:
        return score, None
    adjustment = {
        "formula_version": FORMULA_VERSION,
        "decay": DECAY,
        "floor": FLOOR,
        "veto": False,
        "per_rule": per_rule,
    }
    return score, adjustment
