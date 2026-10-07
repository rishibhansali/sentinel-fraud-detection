"""Stable, dependency-free transaction features for the anomaly baseline."""

import math
from collections.abc import Sequence

FEATURE_NAMES = tuple(f"v{i}" for i in range(1, 29)) + ("log_amount",)


def extract_features(v_values: Sequence[object], amount: object) -> tuple[float, ...]:
    """Return V1–V28 unchanged, followed by log1p of nonnegative Amount."""
    if len(v_values) != 28:
        raise ValueError("Expected exactly 28 V values")
    try:
        values = tuple(float(value) for value in v_values)
        numeric_amount = float(amount)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Model inputs must be numeric") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError("V values must be finite")
    if not math.isfinite(numeric_amount) or numeric_amount < 0:
        raise ValueError("Amount must be finite and nonnegative")
    return values + (math.log1p(numeric_amount),)
