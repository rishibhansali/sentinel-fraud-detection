# Sentinel Phase 3 — Deterministic Detection Engine Design

## Purpose

Phase 3 builds the rules-based scoring engine — the piece that makes "no
AI layer, still works" literally true. This phase delivers the scoring
function/service itself, fully tested for determinism and explainability.
It does not wire into a live/streamed pipeline (Phase 4), does not add any
UI for editing thresholds (Phase 9 — backend/data model only here), and
does not touch ML scoring or the isolation forest work (Phases 6-7).

## Architecture

Lives in `backend/app/detection/`, under the existing FastAPI skeleton —
Phase 5 will expose this via an API from that same service, so the code
belongs there now even though no endpoint is built yet.

**Core principle: the scoring function is pure.** It takes already-fetched
Python data in and returns a result. All I/O (DB queries for a
transaction's history, its baseline, current weights) happens outside it,
in a thin separate loader. "Now" for every rule is the transaction's own
`ts`, never wall-clock — this is what makes "identical inputs → identical
output" and full explainability actually testable.

## Interface

```
score_transaction(transaction, recent_transactions, baseline, rules_config) -> ScoreResult
```

- `transaction`: the one being scored — `id, user_id, ts, amount, lat, lon`.
- `recent_transactions`: the user's prior transactions, already fetched,
  sorted most-recent-first.
- `baseline`: one row from Phase 2's `user_transaction_features` view —
  `transaction_count, avg_amount, total_amount, fraud_count,
  last_transaction_at`.
- `rules_config`: current weights/thresholds, already loaded from Postgres.
- Returns `ScoreResult`: a total weighted score plus a list of
  `RuleResult` (one per rule): `fired: bool`, `sub_score: float`, and a
  `details: dict` containing the exact numbers that produced that
  verdict (e.g. `{"count_in_window": 6, "window_minutes": 10,
  "threshold": 5}`). `details` is what explainability tests assert
  against — not just the final number.

## `rules_config` schema

One row per rule, not a single blob, so Phase 5's API and Phase 9's UI
have a natural per-rule edit path:

```sql
CREATE TABLE rules_config (
    rule_name TEXT PRIMARY KEY,
    weight DOUBLE PRECISION NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    params JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`params` holds each rule's own thresholds (shape differs per rule).
`enabled` and `updated_at` exist for the future edit path even though
nothing writes to them yet in this phase.

## The three rules

**Sub-scores are never capped.** `min(x, 1.0)` is NOT applied anywhere in
the rules below — severity above threshold must stay visible (e.g. a
velocity of 50 against a threshold of 5 must score far higher than a
velocity of 6 against the same threshold). Only `total_score` may be
capped/normalized for display purposes, at implementation's discretion;
individual rule `sub_score` values never are.

1. **Velocity** — count of `recent_transactions` within `params.window_minutes`
   before `transaction.ts`, PLUS the transaction being scored itself (a
   `threshold_count` of 5 means 5 transactions total in the window,
   including the current one). Fires when
   `len(matching_recent_transactions) + 1 >= threshold_count`.
   `sub_score = (len(matching_recent_transactions) + 1) / threshold_count`
   (uncapped).

2. **Amount vs. baseline** — relative deviation
   `(amount - avg_amount) / avg_amount` against `baseline.avg_amount`.
   **Confirmed decision:** `user_transaction_features` has no standard
   deviation, only `avg_amount` — this rule uses "multiples of the mean"
   rather than a proper z-score, and the view is used exactly as Phase 2
   built it (no schema change). Fires if `abs(relative_deviation) >=
   params.deviation_multiplier`. If `baseline.transaction_count == 0`
   (no history), the rule cannot fire — `fired=False, sub_score=0.0`,
   an explicit tested edge case, not a silent failure.
   `sub_score = abs(relative_deviation) / deviation_multiplier` (uncapped).

3. **Geo-impossibility** — compares `transaction` against the single most
   recent prior transaction: great-circle distance ÷ elapsed hours =
   implied speed. Fires if implied speed exceeds `params.max_speed_kmh`
   (e.g. ~900 km/h). **Confirmed decision:** a minimum-distance guard
   (`params.min_distance_km`, e.g. 50km) skips the speed check entirely
   below that distance, so ordinary local movement / GPS jitter near a
   user's home never trips it. If there is no prior transaction, the
   rule cannot fire (same explicit-edge-case treatment as rule 2).

   **Confirmed decision — zero elapsed time:** if the two transactions
   have identical timestamps (`elapsed_hours == 0`) AND the distance
   exceeds `params.min_distance_km`, this is an automatic fire — two
   simultaneous transactions in two distant places is the single most
   fraud-relevant case this rule can see, and it can't be reached by
   computing `distance / elapsed_hours` (division by zero). Branch on
   `elapsed_hours == 0` explicitly before any division; do not compute
   implied speed in that case. `sub_score` in this branch is
   `distance_km / min_distance_km` (uncapped) — there is no speed to
   express severity with, so distance-over-guard stands in for it.

   **Reuse note:** `scripts/augment.py` has the *destination-point*
   formula (start point + distance + bearing → end point) — the forward
   problem Phase 1 needed. Geo-impossibility needs the *inverse* problem
   (two points → distance), which doesn't exist as reusable code yet, and
   `scripts/` and `backend/` are separate services with separate venvs in
   this project's architecture, so there's no live import path between
   them. What's actually reused is the same `EARTH_RADIUS_KM = 6371.0`
   constant and the same non-flat-earth rigor, in a new
   `backend/app/detection/geo.py` haversine distance function — not a
   literal shared import.

## Combining rule scores

```
total_score = sum(weight * sub_score for enabled rules) / sum(weight for enabled rules)
```

Normalizing by enabled weight keeps the total comparable regardless of
how many rules are on or how weights are tuned. Disabled rules
(`enabled=false`) are excluded from both the sum and the weight total.

**Confirmed decision — zero enabled rules:** if every rule is disabled,
`sum(weight for enabled rules) == 0`, which would divide by zero. This
is guarded explicitly (not left to fall out of float behavior):
`total_score = 0.0` and no rule is treated as fired when no rule is
enabled. Tested explicitly.

**Confirmed decision — disabled rules are never omitted from the output.**
Every rule in `rules_config` produces a `RuleResult` in `ScoreResult`,
whether enabled or not. A disabled rule's `RuleResult` is always
`fired=False, sub_score=0.0, details={"skipped": "rule disabled"}` — the
full rule set is always visible, not just the active subset.

## Loading `rules_config` from Postgres

A small `load_rules_config()` function queries the table into typed
config objects — a read path Phase 4/5 will call later. Tested against
the real running Postgres (matching this project's established pattern
from Phases 1-2), not mocked. No write/edit path is built (Phase 5's API
/ Phase 9's UI).

## Determinism and auditability (the core deliverable)

- Unit tests proving identical inputs always produce identical scores.
- Unit tests proving the score breakdown is fully explainable: given a
  transaction, recent history, and current weights, assert exactly which
  rules fired and why (via `details`), not just the final number.
- No hidden state, no randomness, no reliance on wall-clock time inside
  `score_transaction` or any per-rule function — every rule receives
  `transaction.ts` as its reference point, nothing calls
  `datetime.now()`.

## Explicitly out of scope

Wiring into a live/streamed pipeline (Phase 4), any UI for editing
thresholds (Phase 9), any ML scoring or isolation forest work (Phases
6-7), any write/edit API for `rules_config` (Phase 5).
