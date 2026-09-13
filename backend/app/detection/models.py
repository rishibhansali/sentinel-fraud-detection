"""Pure data types for the detection engine. All frozen (immutable) so a
ScoreResult can never be mutated after scoring — determinism by construction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Transaction:
    id: int
    user_id: int
    ts: datetime
    amount: float
    lat: float
    lon: float


@dataclass(frozen=True)
class UserBaseline:
    transaction_count: int
    avg_amount: float
    total_amount: float
    fraud_count: int
    last_transaction_at: datetime | None


@dataclass(frozen=True)
class RuleConfig:
    rule_name: str
    weight: float
    enabled: bool
    params: dict


@dataclass(frozen=True)
class RuleResult:
    rule_name: str
    fired: bool
    sub_score: float
    details: dict


@dataclass(frozen=True)
class ScoreResult:
    total_score: float
    rule_results: list[RuleResult]
