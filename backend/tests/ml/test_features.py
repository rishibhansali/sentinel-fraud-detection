from decimal import Decimal
import math

import pytest

from app.ml.features import FEATURE_NAMES, extract_features


def test_feature_order_and_log_amount():
    assert FEATURE_NAMES == tuple(f"v{i}" for i in range(1, 29)) + ("log_amount",)
    assert extract_features(range(1, 29), Decimal("99")) == (
        tuple(float(i) for i in range(1, 29)) + (math.log1p(99),)
    )


@pytest.mark.parametrize(
    "values,amount",
    [
        ([0] * 27, 1),
        ([0] * 29, 1),
        ([float("nan")] + [0] * 27, 1),
        ([0] * 28, -1),
        ([0] * 28, float("inf")),
        (["bad"] + [0] * 27, 1),
    ],
)
def test_invalid_inputs_fail(values, amount):
    with pytest.raises(ValueError):
        extract_features(values, amount)
