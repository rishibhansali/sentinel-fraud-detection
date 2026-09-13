"""Geo-impossibility rule: flags a transaction that couldn't physically
have happened given the user's most recent prior transaction's location
and elapsed time (implied speed), or an automatic fire when two
transactions share an identical timestamp but are far apart (confirmed
decision — the zero-elapsed-time case is the single most fraud-relevant
case this rule can see, and can't be reached via distance/elapsed_hours).
"""
from .geo import haversine_distance_km
from .models import RuleConfig, RuleResult, Transaction


def score_geo_impossibility(
    transaction: Transaction,
    recent_transactions: list[Transaction],
    config: RuleConfig,
) -> RuleResult:
    if not config.enabled:
        return RuleResult(
            rule_name=config.rule_name, fired=False, sub_score=0.0,
            details={"skipped": "rule disabled"},
        )

    if not recent_transactions:
        return RuleResult(
            rule_name=config.rule_name,
            fired=False,
            sub_score=0.0,
            details={"reason": "no prior transaction for user"},
        )

    previous = recent_transactions[0]
    distance_km = haversine_distance_km(previous.lat, previous.lon, transaction.lat, transaction.lon)
    min_distance_km = config.params["min_distance_km"]
    max_speed_kmh = config.params["max_speed_kmh"]

    if distance_km < min_distance_km:
        return RuleResult(
            rule_name=config.rule_name,
            fired=False,
            sub_score=0.0,
            details={
                "distance_km": distance_km,
                "min_distance_km": min_distance_km,
                "reason": "distance below minimum guard",
            },
        )

    elapsed_hours = (transaction.ts - previous.ts).total_seconds() / 3600.0

    if elapsed_hours == 0:
        return RuleResult(
            rule_name=config.rule_name,
            fired=True,
            sub_score=distance_km / min_distance_km,
            details={
                "distance_km": distance_km,
                "min_distance_km": min_distance_km,
                "elapsed_hours": 0.0,
                "reason": "identical timestamps, distance exceeds minimum guard",
            },
        )

    implied_speed_kmh = distance_km / elapsed_hours
    fired = implied_speed_kmh > max_speed_kmh
    sub_score = implied_speed_kmh / max_speed_kmh

    return RuleResult(
        rule_name=config.rule_name,
        fired=fired,
        sub_score=sub_score,
        details={
            "distance_km": distance_km,
            "elapsed_hours": elapsed_hours,
            "implied_speed_kmh": implied_speed_kmh,
            "max_speed_kmh": max_speed_kmh,
        },
    )
