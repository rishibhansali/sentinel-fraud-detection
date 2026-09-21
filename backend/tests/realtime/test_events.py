"""Pure tests for case_summary / case_event (no Redis, no DB)."""
import json
from datetime import datetime, timezone

import pytest

from app.realtime.events import case_event, case_summary

RULES = [
    {"rule_name": "velocity", "fired": True, "sub_score": 40, "details": {}},
    {"rule_name": "geo", "fired": False, "sub_score": 0, "details": {}},
    {"rule_name": "amount", "fired": True, "sub_score": 20, "details": {}},
]


def _row(rule_results):
    return {
        "id": 7,
        "transaction_id": "tx-1",
        "user_id": "u-1",
        "total_score": 60.0,
        "priority_score": 55.0,
        "status": "open",
        "claimed_by": None,
        "rule_results": rule_results,
        "ml_anomaly_score": None,
        "flagged_at": datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        "extra_column": "ignored",
    }


@pytest.mark.parametrize("as_str", [True, False])
def test_case_summary_shape_and_fired_rules(as_str):
    rr = json.dumps(RULES) if as_str else RULES
    s = case_summary(_row(rr))
    assert s == {
        "id": 7, "transaction_id": "tx-1", "user_id": "u-1",
        "total_score": 60.0, "priority_score": 55.0, "status": "open",
        "claimed_by": None, "fired_rules": ["velocity", "amount"],
        "ml_anomaly_score": None,
        "flagged_at": "2025-01-01T12:00:00+00:00",
    }
    json.dumps(s)  # JSON-serializable


def test_case_summary_flagged_at_string_passthrough():
    row = _row(RULES)
    row["flagged_at"] = "2025-01-01T12:00:00+00:00"
    assert case_summary(row)["flagged_at"] == "2025-01-01T12:00:00+00:00"


@pytest.mark.parametrize("etype", ["case.created", "case.updated"])
def test_case_event_wraps(etype):
    s = case_summary(_row(RULES))
    assert case_event(etype, s) == {"type": etype, "case": s}


def test_case_event_bad_type():
    with pytest.raises(ValueError):
        case_event("case.deleted", {})
