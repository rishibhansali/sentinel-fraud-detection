"""Pure builders for case events (spec 5.1). No I/O."""
import json

EVENT_TYPES = ("case.created", "case.updated")


def case_summary(row: dict) -> dict:
    rule_results = row["rule_results"]
    if isinstance(rule_results, (str, bytes)):
        rule_results = json.loads(rule_results)
    flagged_at = row["flagged_at"]
    if hasattr(flagged_at, "isoformat"):
        flagged_at = flagged_at.isoformat()
    return {
        "id": row["id"],
        "transaction_id": row["transaction_id"],
        "user_id": row["user_id"],
        "total_score": row["total_score"],
        "priority_score": row["priority_score"],
        "status": row["status"],
        "claimed_by": row.get("claimed_by"),
        "fired_rules": [r["rule_name"] for r in (rule_results or []) if r.get("fired")],
        "ml_anomaly_score": row.get("ml_anomaly_score"),
        "flagged_at": flagged_at,
    }


def case_event(event_type: str, summary: dict) -> dict:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"event_type must be one of {EVENT_TYPES}, got {event_type!r}")
    return {"type": event_type, "case": summary}
