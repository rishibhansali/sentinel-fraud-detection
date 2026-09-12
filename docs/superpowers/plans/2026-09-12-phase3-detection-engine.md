# Sentinel Phase 3 Detection Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, fully explainable rules-based fraud scoring engine (velocity, amount-vs-baseline, geo-impossibility) with configurable weights/thresholds stored in Postgres, proven correct by unit tests — no live pipeline wiring, no UI, no ML in this phase.

**Architecture:** Pure Python functions under `backend/app/detection/` — one small module per rule, a combiner (`scoring.py`) that weights and normalizes their outputs, and a thin Postgres loader (`config.py`) for the `rules_config` table. No function in the scoring path touches the database or wall-clock time; all I/O and "now" are supplied by the caller.

**Tech Stack:** Python 3.12 (existing `backend/.venv`), pytest, psycopg2 (new dependency for `backend/requirements.txt`), Postgres 16 (existing).

## Global Constraints

- `score_transaction()` and every rule function are pure: no DB access, no `datetime.now()`, no randomness. Callers supply `transaction.ts` as the sole time reference.
- Sub-scores are **never capped** (no `min(x, 1.0)` anywhere in rule logic). Only `total_score` may be capped/normalized, and this plan does not do so.
- `rules_config`: one row per rule (not a blob), columns `rule_name TEXT PRIMARY KEY, weight DOUBLE PRECISION NOT NULL, enabled BOOLEAN NOT NULL DEFAULT true, params JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
- Amount-vs-baseline uses `user_transaction_features`'s `avg_amount` only (no stddev) — "multiples of the mean," not a z-score. This is confirmed, not a gap to fix.
- Geo-impossibility reimplements haversine distance in `backend/app/detection/geo.py` rather than importing from `scripts/augment.py` (separate services, separate venvs) — but reuses the same `EARTH_RADIUS_KM = 6371.0` constant and non-flat-earth rigor.
- Five confirmed decisions, each with its own explicit test (not incidental coverage):
  1. Zero enabled rules → `total_score = 0.0`, guarded explicitly (not left to a 0/0 division).
  2. Geo rule with `elapsed_hours == 0` and distance beyond `min_distance_km` → automatic fire; branch before dividing, never compute `distance / elapsed_hours` when `elapsed_hours == 0`.
  3. Disabled rules are never omitted from `ScoreResult.rule_results` — always `fired=False, sub_score=0.0, details={"skipped": "rule disabled"}`.
  4. Sub-scores uncapped (see above).
  5. Velocity counts the transaction being scored as part of its own window: fires when `len(matching_recent_transactions) + 1 >= threshold_count`.
- Two no-history edge cases, each explicit and tested: amount-baseline cannot fire when `baseline.transaction_count == 0`; geo-impossibility cannot fire when there is no prior transaction.
- `load_rules_config()` is tested against the real running Postgres container (matching Phases 1-2's established pattern), not mocked.
- No Claude/Anthropic co-author trailer in any commit message. Git identity is already configured to `Rishi <rishi.jaguars@gmail.com>`.
- Out of scope: live/streamed pipeline wiring (Phase 4), any UI or write/edit API for `rules_config` (Phase 5/9), ML/isolation-forest scoring (Phases 6-7).

---

### Task 1: Data models + haversine geo module

**Files:**
- Create: `backend/app/detection/__init__.py` (empty)
- Create: `backend/app/detection/models.py`
- Create: `backend/app/detection/geo.py`
- Test: `backend/tests/detection/__init__.py` (empty)
- Test: `backend/tests/detection/test_geo.py`

**Interfaces:**
- Produces (consumed by every later task): `Transaction(id, user_id, ts, amount, lat, lon)`, `UserBaseline(transaction_count, avg_amount, total_amount, fraud_count, last_transaction_at)`, `RuleConfig(rule_name, weight, enabled, params)`, `RuleResult(rule_name, fired, sub_score, details)`, `ScoreResult(total_score, rule_results)` — all frozen dataclasses in `backend/app/detection/models.py`.
- Produces: `EARTH_RADIUS_KM: float` and `haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float` in `backend/app/detection/geo.py`.

- [ ] **Step 1: Write the failing tests — `backend/tests/detection/test_geo.py`**

```python
import math

from app.detection.geo import EARTH_RADIUS_KM, haversine_distance_km


def test_haversine_distance_zero_for_identical_points():
    assert haversine_distance_km(40.0, -73.0, 40.0, -73.0) == 0.0


def test_haversine_distance_quarter_great_circle():
    # (0,0) to (0,90) is a quarter of the great circle: (pi/2) * R
    expected = (math.pi / 2) * EARTH_RADIUS_KM
    actual = haversine_distance_km(0.0, 0.0, 0.0, 90.0)
    assert abs(actual - expected) < 0.01


def test_haversine_distance_symmetric_and_matches_known_value():
    d1 = haversine_distance_km(51.5074, -0.1278, 40.7128, -74.0060)  # London-NYC
    d2 = haversine_distance_km(40.7128, -74.0060, 51.5074, -0.1278)
    assert abs(d1 - d2) < 1e-9
    assert 5500 < d1 < 5600  # known real-world great-circle distance ~5570km
```

Also create empty `backend/tests/detection/__init__.py`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_geo.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.detection'`.

- [ ] **Step 3: Write `backend/app/detection/__init__.py`** (empty file)

- [ ] **Step 4: Write `backend/app/detection/models.py`**

```python
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
```

- [ ] **Step 5: Write `backend/app/detection/geo.py`**

```python
"""Great-circle distance between two points (haversine) — the inverse of
Phase 1's destination-point problem (scripts/augment.py computes a
destination given a start point, distance, and bearing; this computes
distance given two points). Not imported from scripts/augment.py:
scripts/ and backend/ are separate services with separate venvs in this
project's architecture, so there is no live import path between them.
Reuses the same EARTH_RADIUS_KM constant and the same non-flat-earth
rigor as Phase 1's math.
"""
import math

EARTH_RADIUS_KM = 6371.0


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_KM * c
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_geo.py -v
```

Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add backend/app/detection/__init__.py backend/app/detection/models.py backend/app/detection/geo.py backend/tests/detection/__init__.py backend/tests/detection/test_geo.py
git commit -m "Add detection engine data models and haversine geo distance function"
```

---

### Task 2: The three rule functions

**Files:**
- Create: `backend/app/detection/velocity.py`
- Create: `backend/app/detection/amount_baseline.py`
- Create: `backend/app/detection/geo_impossibility.py`
- Test: `backend/tests/detection/test_velocity.py`
- Test: `backend/tests/detection/test_amount_baseline.py`
- Test: `backend/tests/detection/test_geo_impossibility.py`

**Interfaces:**
- Consumes: `Transaction`, `UserBaseline`, `RuleConfig`, `RuleResult` from Task 1's `backend/app/detection/models.py`; `haversine_distance_km` from Task 1's `backend/app/detection/geo.py`.
- Produces (consumed by Task 3): `score_velocity(transaction: Transaction, recent_transactions: list[Transaction], config: RuleConfig) -> RuleResult`; `score_amount_baseline(transaction: Transaction, baseline: UserBaseline, config: RuleConfig) -> RuleResult`; `score_geo_impossibility(transaction: Transaction, recent_transactions: list[Transaction], config: RuleConfig) -> RuleResult`. All three: when `config.enabled` is `False`, return `RuleResult(rule_name=config.rule_name, fired=False, sub_score=0.0, details={"skipped": "rule disabled"})` without evaluating any rule logic.

#### Velocity

- [ ] **Step 1: Write the failing tests — `backend/tests/detection/test_velocity.py`**

```python
from datetime import datetime, timedelta, timezone

from app.detection.models import RuleConfig, Transaction
from app.detection.velocity import score_velocity

BASE_TS = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _txn(minutes_ago: int, id_: int = 1) -> Transaction:
    return Transaction(
        id=id_, user_id=1, ts=BASE_TS - timedelta(minutes=minutes_ago),
        amount=10.0, lat=0.0, lon=0.0,
    )


def _current() -> Transaction:
    return Transaction(id=99, user_id=1, ts=BASE_TS, amount=10.0, lat=0.0, lon=0.0)


def _config(window_minutes=10, threshold_count=5, enabled=True) -> RuleConfig:
    return RuleConfig(
        rule_name="velocity", weight=1.0, enabled=enabled,
        params={"window_minutes": window_minutes, "threshold_count": threshold_count},
    )


def test_velocity_fires_when_count_including_current_meets_threshold():
    recent = [_txn(1), _txn(2), _txn(3), _txn(4)]  # 4 prior, all within 10 min

    result = score_velocity(_current(), recent, _config())

    assert result.fired is True
    assert result.details["count_in_window"] == 5
    assert result.sub_score == 1.0


def test_velocity_does_not_fire_below_threshold():
    recent = [_txn(1), _txn(2), _txn(3)]  # 3 prior -> 4 total, threshold 5

    result = score_velocity(_current(), recent, _config())

    assert result.fired is False
    assert result.details["count_in_window"] == 4


def test_velocity_excludes_transactions_outside_window():
    recent = [_txn(1), _txn(2), _txn(30)]  # 30-min-old one is outside the 10-min window

    result = score_velocity(_current(), recent, _config(threshold_count=3))

    assert result.details["count_in_window"] == 3  # 2 in-window + current
    assert result.fired is True


def test_velocity_sub_score_uncapped_above_threshold():
    recent = [_txn(m) for m in range(1, 50)]  # 49 prior, all within a 60-min window

    result = score_velocity(_current(), recent, _config(window_minutes=60, threshold_count=5))

    assert result.details["count_in_window"] == 50
    assert result.sub_score == 10.0  # 50 / 5, uncapped
    assert result.sub_score > 1.0


def test_velocity_disabled_rule_returns_skipped():
    result = score_velocity(_current(), [], _config(enabled=False))

    assert result.fired is False
    assert result.sub_score == 0.0
    assert result.details == {"skipped": "rule disabled"}
```

- [ ] **Step 2: Run to verify failure**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_velocity.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.detection.velocity'`.

- [ ] **Step 3: Write `backend/app/detection/velocity.py`**

```python
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
```

- [ ] **Step 4: Run to verify pass**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_velocity.py -v
```

Expected: PASS (5 tests).

#### Amount vs. baseline

- [ ] **Step 5: Write the failing tests — `backend/tests/detection/test_amount_baseline.py`**

```python
from datetime import datetime, timezone

from app.detection.amount_baseline import score_amount_baseline
from app.detection.models import RuleConfig, Transaction, UserBaseline

TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _config(deviation_multiplier=3.0, enabled=True) -> RuleConfig:
    return RuleConfig(
        rule_name="amount_baseline", weight=1.0, enabled=enabled,
        params={"deviation_multiplier": deviation_multiplier},
    )


def _baseline(transaction_count=10, avg_amount=100.0) -> UserBaseline:
    return UserBaseline(
        transaction_count=transaction_count, avg_amount=avg_amount,
        total_amount=avg_amount * transaction_count, fraud_count=0,
        last_transaction_at=TS,
    )


def test_amount_baseline_fires_on_large_deviation():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=400.0, lat=0.0, lon=0.0)

    result = score_amount_baseline(txn, _baseline(avg_amount=100.0), _config(deviation_multiplier=3.0))

    # relative deviation = (400-100)/100 = 3.0, >= 3.0 -> fires
    assert result.fired is True
    assert result.details["relative_deviation"] == 3.0
    assert result.sub_score == 1.0


def test_amount_baseline_does_not_fire_within_normal_range():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=120.0, lat=0.0, lon=0.0)

    result = score_amount_baseline(txn, _baseline(avg_amount=100.0), _config(deviation_multiplier=3.0))

    assert result.fired is False


def test_amount_baseline_sub_score_uncapped():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=1000.0, lat=0.0, lon=0.0)

    result = score_amount_baseline(txn, _baseline(avg_amount=100.0), _config(deviation_multiplier=3.0))

    # relative deviation = 9.0, sub_score = 9.0 / 3.0 = 3.0, uncapped
    assert result.sub_score == 3.0


def test_amount_baseline_no_history_cannot_fire():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=1_000_000.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=0, avg_amount=0.0, total_amount=0.0,
                             fraud_count=0, last_transaction_at=None)

    result = score_amount_baseline(txn, baseline, _config())

    assert result.fired is False
    assert result.sub_score == 0.0
    assert "no usable baseline" in result.details["reason"]


def test_amount_baseline_zero_avg_amount_guarded():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=50.0, lat=0.0, lon=0.0)
    baseline = _baseline(transaction_count=3, avg_amount=0.0)

    result = score_amount_baseline(txn, baseline, _config())

    assert result.fired is False
    assert result.sub_score == 0.0
    assert "no usable baseline" in result.details["reason"]


def test_amount_baseline_disabled_rule_returns_skipped():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=50.0, lat=0.0, lon=0.0)

    result = score_amount_baseline(txn, _baseline(), _config(enabled=False))

    assert result.details == {"skipped": "rule disabled"}
```

- [ ] **Step 6: Run to verify failure**, then

- [ ] **Step 7: Write `backend/app/detection/amount_baseline.py`**

```python
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
```

- [ ] **Step 8: Run to verify pass**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_amount_baseline.py -v
```

Expected: PASS (6 tests).

#### Geo-impossibility

- [ ] **Step 9: Write the failing tests — `backend/tests/detection/test_geo_impossibility.py`**

```python
from datetime import datetime, timedelta, timezone

from app.detection.geo_impossibility import score_geo_impossibility
from app.detection.models import RuleConfig, Transaction

TS = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)


def _config(min_distance_km=50.0, max_speed_kmh=900.0, enabled=True) -> RuleConfig:
    return RuleConfig(
        rule_name="geo_impossibility", weight=1.0, enabled=enabled,
        params={"min_distance_km": min_distance_km, "max_speed_kmh": max_speed_kmh},
    )


def test_geo_fires_on_impossible_speed():
    previous = Transaction(id=1, user_id=1, ts=TS, amount=10.0, lat=NYC[0], lon=NYC[1])
    current = Transaction(id=2, user_id=1, ts=TS + timedelta(minutes=30), amount=10.0, lat=LONDON[0], lon=LONDON[1])

    result = score_geo_impossibility(current, [previous], _config())

    assert result.fired is True
    assert result.details["implied_speed_kmh"] > 900.0


def test_geo_does_not_fire_within_min_distance_guard():
    previous = Transaction(id=1, user_id=1, ts=TS, amount=10.0, lat=40.0, lon=-74.0)
    current = Transaction(id=2, user_id=1, ts=TS + timedelta(minutes=1), amount=10.0, lat=40.001, lon=-74.001)

    result = score_geo_impossibility(current, [previous], _config())

    assert result.fired is False
    assert result.details["reason"] == "distance below minimum guard"


def test_geo_zero_elapsed_time_automatic_fire():
    previous = Transaction(id=1, user_id=1, ts=TS, amount=10.0, lat=NYC[0], lon=NYC[1])
    current = Transaction(id=2, user_id=1, ts=TS, amount=10.0, lat=LONDON[0], lon=LONDON[1])  # same ts

    result = score_geo_impossibility(current, [previous], _config())

    assert result.fired is True
    assert result.details["elapsed_hours"] == 0.0
    assert "identical timestamps" in result.details["reason"]
    assert result.sub_score > 1.0


def test_geo_no_prior_transaction_cannot_fire():
    current = Transaction(id=2, user_id=1, ts=TS, amount=10.0, lat=LONDON[0], lon=LONDON[1])

    result = score_geo_impossibility(current, [], _config())

    assert result.fired is False
    assert result.sub_score == 0.0
    assert "no prior transaction" in result.details["reason"]


def test_geo_disabled_rule_returns_skipped():
    current = Transaction(id=2, user_id=1, ts=TS, amount=10.0, lat=LONDON[0], lon=LONDON[1])

    result = score_geo_impossibility(current, [], _config(enabled=False))

    assert result.details == {"skipped": "rule disabled"}
```

- [ ] **Step 10: Run to verify failure**, then

- [ ] **Step 11: Write `backend/app/detection/geo_impossibility.py`**

```python
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
```

- [ ] **Step 12: Run to verify pass**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_geo_impossibility.py -v
```

Expected: PASS (5 tests).

- [ ] **Step 13: Run all of Task 2's tests together, then commit**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_velocity.py tests/detection/test_amount_baseline.py tests/detection/test_geo_impossibility.py -v
```

Expected: PASS (16 tests total).

```bash
git add backend/app/detection/velocity.py backend/app/detection/amount_baseline.py backend/app/detection/geo_impossibility.py backend/tests/detection/test_velocity.py backend/tests/detection/test_amount_baseline.py backend/tests/detection/test_geo_impossibility.py
git commit -m "Add velocity, amount-baseline, and geo-impossibility rule functions"
```

---

### Task 3: `score_transaction()` combiner

**Files:**
- Create: `backend/app/detection/scoring.py`
- Test: `backend/tests/detection/test_scoring.py`

**Interfaces:**
- Consumes: `score_velocity`, `score_amount_baseline`, `score_geo_impossibility` from Task 2; `Transaction, UserBaseline, RuleConfig, ScoreResult` from Task 1.
- Produces (consumed by Task 5): `score_transaction(transaction: Transaction, recent_transactions: list[Transaction], baseline: UserBaseline, rules_config: dict[str, RuleConfig]) -> ScoreResult`. `rules_config` is keyed by exactly `"velocity"`, `"amount_baseline"`, `"geo_impossibility"`.

- [ ] **Step 1: Write the failing tests — `backend/tests/detection/test_scoring.py`**

```python
from datetime import datetime, timezone

from app.detection.models import RuleConfig, Transaction, UserBaseline
from app.detection.scoring import score_transaction

TS = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _all_enabled_config() -> dict[str, RuleConfig]:
    return {
        "velocity": RuleConfig("velocity", weight=1.0, enabled=True,
                                params={"window_minutes": 10, "threshold_count": 5}),
        "amount_baseline": RuleConfig("amount_baseline", weight=1.0, enabled=True,
                                       params={"deviation_multiplier": 3.0}),
        "geo_impossibility": RuleConfig("geo_impossibility", weight=1.0, enabled=True,
                                         params={"min_distance_km": 50.0, "max_speed_kmh": 900.0}),
    }


def test_score_transaction_no_rules_fire_when_nothing_anomalous():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=5, avg_amount=100.0, total_amount=500.0,
                             fraud_count=0, last_transaction_at=TS)

    result = score_transaction(txn, [], baseline, _all_enabled_config())

    assert len(result.rule_results) == 3
    assert all(r.fired is False for r in result.rule_results)
    # No rule fires, but velocity's sub_score is never exactly 0.0 even with
    # zero history: it counts the transaction being scored as part of its
    # own window (confirmed decision), so a single transaction always
    # contributes 1/threshold_count. "Nothing fired" does not mean
    # "total_score == 0.0" — that's expected, not a bug.
    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert velocity_result.sub_score == 0.2  # 1 transaction / threshold_count 5, self-inclusive
    expected_total = (velocity_result.sub_score + amount_result.sub_score + geo_result.sub_score) / 3
    assert abs(result.total_score - expected_total) < 1e-9


def test_score_transaction_zero_enabled_rules_guard():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=5, avg_amount=100.0, total_amount=500.0,
                             fraud_count=0, last_transaction_at=TS)
    config = {
        name: RuleConfig(name, weight=cfg.weight, enabled=False, params=cfg.params)
        for name, cfg in _all_enabled_config().items()
    }

    result = score_transaction(txn, [], baseline, config)

    assert result.total_score == 0.0
    assert len(result.rule_results) == 3
    assert all(r.fired is False for r in result.rule_results)
    assert all(r.details == {"skipped": "rule disabled"} for r in result.rule_results)


def test_score_transaction_disabled_rule_excluded_from_weighted_average():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=1000.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=5, avg_amount=100.0, total_amount=500.0,
                             fraud_count=0, last_transaction_at=TS)
    config = _all_enabled_config()
    config["velocity"] = RuleConfig("velocity", weight=1.0, enabled=False,
                                     params={"window_minutes": 10, "threshold_count": 5})

    result = score_transaction(txn, [], baseline, config)

    rule_names = {r.rule_name for r in result.rule_results}
    assert rule_names == {"velocity", "amount_baseline", "geo_impossibility"}

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    assert velocity_result.details == {"skipped": "rule disabled"}

    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    expected_total = (amount_result.sub_score + geo_result.sub_score) / 2  # velocity excluded, equal weights
    assert abs(result.total_score - expected_total) < 1e-9


def test_score_transaction_weighted_average_respects_unequal_weights():
    txn = Transaction(id=1, user_id=1, ts=TS, amount=1000.0, lat=0.0, lon=0.0)  # triggers amount_baseline
    baseline = UserBaseline(transaction_count=5, avg_amount=100.0, total_amount=500.0,
                             fraud_count=0, last_transaction_at=TS)
    config = _all_enabled_config()
    config["amount_baseline"] = RuleConfig("amount_baseline", weight=3.0, enabled=True,
                                            params={"deviation_multiplier": 3.0})

    result = score_transaction(txn, [], baseline, config)

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    total_weight = 1.0 + 3.0 + 1.0
    expected_total = (
        1.0 * velocity_result.sub_score + 3.0 * amount_result.sub_score + 1.0 * geo_result.sub_score
    ) / total_weight
    assert abs(result.total_score - expected_total) < 1e-9
```

- [ ] **Step 2: Run to verify failure**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_scoring.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.detection.scoring'`.

- [ ] **Step 3: Write `backend/app/detection/scoring.py`**

```python
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
```

- [ ] **Step 4: Run to verify pass**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_scoring.py -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/detection/scoring.py backend/tests/detection/test_scoring.py
git commit -m "Add score_transaction combiner with zero-enabled-rules guard"
```

---

### Task 4: `rules_config` migration + `load_rules_config()`

**Files:**
- Create: `infra/migrations/005_create_rules_config.sql`
- Create: `backend/app/detection/config.py`
- Modify: `backend/requirements.txt`
- Test: `backend/tests/detection/test_config.py`

**Interfaces:**
- Produces: a `rules_config` table seeded with the three rules' default weights/params, and `load_rules_config(dsn: str) -> dict[str, RuleConfig]` — the read path Phase 4/5 will call. `RuleConfig` is from Task 1's `models.py`.

- [ ] **Step 1: Add `psycopg2-binary` to `backend/requirements.txt`**

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
httpx==0.27.2
pytest==8.3.3
psycopg2-binary==2.9.12
```

```bash
backend/.venv/bin/pip install -r backend/requirements.txt
```

- [ ] **Step 2: Write `infra/migrations/005_create_rules_config.sql`**

```sql
\set ON_ERROR_STOP on
-- One row per detection rule, not a single blob, so Phase 5's API and
-- Phase 9's UI have a natural per-rule edit path. No write/edit path is
-- built in this phase — only the read side (load_rules_config()) exists.
BEGIN;

CREATE TABLE rules_config (
    rule_name TEXT PRIMARY KEY,
    weight DOUBLE PRECISION NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    params JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO rules_config (rule_name, weight, params) VALUES
    ('velocity', 1.0, '{"window_minutes": 10, "threshold_count": 5}'),
    ('amount_baseline', 1.0, '{"deviation_multiplier": 3.0}'),
    ('geo_impossibility', 1.0, '{"min_distance_km": 50.0, "max_speed_kmh": 900.0}');

COMMIT;
```

- [ ] **Step 3: Apply the migration**

```bash
docker compose -f infra/docker-compose.yml exec -T db psql -U sentinel -d sentinel -f - < infra/migrations/005_create_rules_config.sql
```

- [ ] **Step 4: Verify the seeded data**

```bash
docker compose -f infra/docker-compose.yml exec -T db psql -U sentinel -d sentinel -c "SELECT rule_name, weight, enabled, params FROM rules_config ORDER BY rule_name;"
```

Expected: 3 rows, all `enabled = t`, `weight = 1`, params matching the INSERT above.

- [ ] **Step 5: Write the failing test — `backend/tests/detection/test_config.py`**

```python
"""Integration test for load_rules_config() against the real running
Postgres instance (matching this project's established testing pattern
from Phases 1-2) — not mocked.
"""
import os

from app.detection.config import load_rules_config

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)


def test_load_rules_config_returns_all_seeded_rules():
    config = load_rules_config(DB_DSN)

    assert set(config.keys()) == {"velocity", "amount_baseline", "geo_impossibility"}
    assert config["velocity"].enabled is True
    assert config["velocity"].weight == 1.0
    assert config["velocity"].params["window_minutes"] == 10
    assert config["velocity"].params["threshold_count"] == 5
    assert config["amount_baseline"].params["deviation_multiplier"] == 3.0
    assert config["geo_impossibility"].params["min_distance_km"] == 50.0
    assert config["geo_impossibility"].params["max_speed_kmh"] == 900.0
```

- [ ] **Step 6: Run to verify failure**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_config.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'app.detection.config'`.

- [ ] **Step 7: Write `backend/app/detection/config.py`**

```python
"""Loads rules_config from Postgres into typed RuleConfig objects — the
read path Phase 4/5 will use to fetch current weights/thresholds. No
write/edit path here (that's Phase 5's API / Phase 9's UI).
"""
import psycopg2
import psycopg2.extras

from .models import RuleConfig


def load_rules_config(dsn: str) -> dict[str, RuleConfig]:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT rule_name, weight, enabled, params FROM rules_config;")
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        row["rule_name"]: RuleConfig(
            rule_name=row["rule_name"],
            weight=row["weight"],
            enabled=row["enabled"],
            params=row["params"],
        )
        for row in rows
    }
```

- [ ] **Step 8: Run to verify pass**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_config.py -v
```

Expected: PASS (1 test).

- [ ] **Step 9: Commit**

```bash
git add infra/migrations/005_create_rules_config.sql backend/app/detection/config.py backend/requirements.txt backend/tests/detection/test_config.py
git commit -m "Add rules_config table and load_rules_config() Postgres loader"
```

---

### Task 5: End-to-end determinism and explainability test suite

**Files:**
- Test: `backend/tests/detection/test_scoring_end_to_end.py`

**Interfaces:**
- Consumes: `score_transaction` from Task 3; `Transaction, UserBaseline, RuleConfig` from Task 1. No new production code — this task is pure test coverage proving the five confirmed decisions and two no-history edge cases hold when exercised through the full `score_transaction()` combiner in realistic multi-rule scenarios, plus the project's core determinism/explainability guarantees.

- [ ] **Step 1: Write `backend/tests/detection/test_scoring_end_to_end.py`**

```python
"""End-to-end determinism and explainability tests for score_transaction(),
covering the five confirmed Phase 3 decisions and the two no-history edge
cases together, through realistic multi-rule scenarios rather than
single-rule isolation (Tasks 2-3 already cover those in isolation).
"""
from datetime import datetime, timedelta, timezone

from app.detection.models import RuleConfig, Transaction, UserBaseline
from app.detection.scoring import score_transaction

TS = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)


def _config(velocity_enabled=True, amount_enabled=True, geo_enabled=True) -> dict[str, RuleConfig]:
    return {
        "velocity": RuleConfig("velocity", weight=1.0, enabled=velocity_enabled,
                                params={"window_minutes": 10, "threshold_count": 5}),
        "amount_baseline": RuleConfig("amount_baseline", weight=1.0, enabled=amount_enabled,
                                       params={"deviation_multiplier": 3.0}),
        "geo_impossibility": RuleConfig("geo_impossibility", weight=1.0, enabled=geo_enabled,
                                         params={"min_distance_km": 50.0, "max_speed_kmh": 900.0}),
    }


def _prior(minutes_ago: int, lat=0.0, lon=0.0, amount=100.0, id_=1) -> Transaction:
    return Transaction(id=id_, user_id=1, ts=TS - timedelta(minutes=minutes_ago),
                        amount=amount, lat=lat, lon=lon)


def test_identical_inputs_produce_identical_scores():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=500.0, lat=LONDON[0], lon=LONDON[1])
    recent = [_prior(1), _prior(2), _prior(3), _prior(4)]
    baseline = UserBaseline(transaction_count=10, avg_amount=100.0, total_amount=1000.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))
    config = _config()

    result1 = score_transaction(txn, recent, baseline, config)
    result2 = score_transaction(txn, recent, baseline, config)

    assert result1 == result2


def test_explainability_all_three_rules_fire_with_exact_details():
    # Velocity: 4 prior in-window + current = 5 >= threshold 5 -> fires.
    # Amount: 500 vs avg 100 -> relative_deviation 4.0 >= 3.0 -> fires.
    # Geo: previous transaction in NYC, current in London 1 minute later -> fires.
    recent = [
        Transaction(id=1, user_id=1, ts=TS - timedelta(minutes=1), amount=100.0, lat=NYC[0], lon=NYC[1]),
        _prior(2), _prior(3), _prior(4),
    ]
    txn = Transaction(id=99, user_id=1, ts=TS, amount=500.0, lat=LONDON[0], lon=LONDON[1])
    baseline = UserBaseline(transaction_count=10, avg_amount=100.0, total_amount=1000.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))

    result = score_transaction(txn, recent, baseline, _config())

    fired_rules = {r.rule_name for r in result.rule_results if r.fired}
    assert fired_rules == {"velocity", "amount_baseline", "geo_impossibility"}

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    assert velocity_result.details["count_in_window"] == 5

    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    assert amount_result.details["relative_deviation"] == 4.0

    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert geo_result.details["implied_speed_kmh"] > 900.0


def test_decision_zero_enabled_rules():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=500.0, lat=LONDON[0], lon=LONDON[1])
    baseline = UserBaseline(transaction_count=10, avg_amount=100.0, total_amount=1000.0,
                             fraud_count=0, last_transaction_at=TS)
    config = _config(velocity_enabled=False, amount_enabled=False, geo_enabled=False)

    result = score_transaction(txn, [], baseline, config)

    assert result.total_score == 0.0
    assert len(result.rule_results) == 3
    assert all(r.fired is False for r in result.rule_results)


def test_decision_geo_zero_elapsed_time_automatic_fire():
    previous = Transaction(id=1, user_id=1, ts=TS, amount=100.0, lat=NYC[0], lon=NYC[1])
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=LONDON[0], lon=LONDON[1])
    baseline = UserBaseline(transaction_count=1, avg_amount=100.0, total_amount=100.0,
                             fraud_count=0, last_transaction_at=TS)

    result = score_transaction(txn, [previous], baseline, _config())

    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert geo_result.fired is True
    assert geo_result.details["elapsed_hours"] == 0.0


def test_decision_disabled_rule_never_omitted():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=1, avg_amount=100.0, total_amount=100.0,
                             fraud_count=0, last_transaction_at=TS)

    result = score_transaction(txn, [], baseline, _config(velocity_enabled=False))

    assert len(result.rule_results) == 3
    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    assert velocity_result.details == {"skipped": "rule disabled"}


def test_decision_sub_scores_uncapped():
    recent = [_prior(m) for m in range(1, 50)]  # 49 prior within a 60-min window
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=50, avg_amount=100.0, total_amount=5000.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))
    config = _config()
    config["velocity"] = RuleConfig("velocity", weight=1.0, enabled=True,
                                     params={"window_minutes": 60, "threshold_count": 5})

    result = score_transaction(txn, recent, baseline, config)

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    assert velocity_result.sub_score == 10.0  # 50 / 5, uncapped, not clamped to 1.0


def test_decision_velocity_counts_current_transaction():
    recent = [_prior(1), _prior(2), _prior(3), _prior(4)]  # exactly 4 prior
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=4, avg_amount=100.0, total_amount=400.0,
                             fraud_count=0, last_transaction_at=TS - timedelta(minutes=1))

    result = score_transaction(txn, recent, baseline, _config())

    velocity_result = next(r for r in result.rule_results if r.rule_name == "velocity")
    # 4 prior + 1 current = 5, threshold 5 -> fires; would NOT fire if only history counted
    assert velocity_result.fired is True
    assert velocity_result.details["count_in_window"] == 5


def test_edge_case_no_transaction_history_for_amount_baseline():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=1_000_000.0, lat=0.0, lon=0.0)
    baseline = UserBaseline(transaction_count=0, avg_amount=0.0, total_amount=0.0,
                             fraud_count=0, last_transaction_at=None)

    result = score_transaction(txn, [], baseline, _config())

    amount_result = next(r for r in result.rule_results if r.rule_name == "amount_baseline")
    assert amount_result.fired is False


def test_edge_case_no_prior_transaction_for_geo():
    txn = Transaction(id=99, user_id=1, ts=TS, amount=100.0, lat=LONDON[0], lon=LONDON[1])
    baseline = UserBaseline(transaction_count=0, avg_amount=0.0, total_amount=0.0,
                             fraud_count=0, last_transaction_at=None)

    result = score_transaction(txn, [], baseline, _config())

    geo_result = next(r for r in result.rule_results if r.rule_name == "geo_impossibility")
    assert geo_result.fired is False
```

- [ ] **Step 2: Run to verify all pass**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/test_scoring_end_to_end.py -v
```

Expected: PASS (9 tests).

- [ ] **Step 3: Run the FULL detection test suite together**

```bash
cd backend && ../backend/.venv/bin/pytest tests/detection/ -v
```

Expected: PASS (33 tests: 3 geo + 5 velocity + 6 amount_baseline + 5 geo_impossibility + 4 scoring + 1 config + 9 end-to-end). Output pristine, no warnings beyond pre-existing FastAPI/starlette deprecation warnings unrelated to this change.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/detection/test_scoring_end_to_end.py
git commit -m "Add end-to-end determinism and explainability test suite for the five confirmed Phase 3 decisions"
git push
```

---

## Self-Review Notes

- **Spec coverage:** pure data models (Task 1), haversine geo reuse-not-import (Task 1), all three rules with disabled-rule handling (Task 2), weighted-average combiner with the zero-enabled guard (Task 3), `rules_config` schema + read-path loader tested against real Postgres (Task 4), and a dedicated end-to-end suite proving determinism plus all five confirmed decisions plus both no-history edge cases (Task 5). All in-scope items are covered; out-of-scope items (live pipeline, UI, ML) are untouched by every task.
- **Type/interface consistency:** all three rule functions share the identical `(transaction, ..., config) -> RuleResult` shape and the identical disabled-rule early return. `score_transaction`'s `rules_config` dict keys (`"velocity"`, `"amount_baseline"`, `"geo_impossibility"`) match the rule names seeded into the `rules_config` table in Task 4 and referenced throughout Task 5's tests.
- **No placeholders:** every code block is complete and runnable against the existing Phase 1/2 infrastructure (Python 3.12 `backend/.venv`, running Postgres 16 container). Two edge cases beyond the five confirmed decisions were added for real correctness (not scope creep): a `baseline.avg_amount == 0` guard in the amount-baseline rule (division-by-zero on plausible data, distinct from the `transaction_count == 0` case) — both fall under the same "no usable baseline" detail message.
