import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ml_dataset import SourceData
from ml_evaluation import evaluate, split_metrics, threshold_for_validation


def sample_data() -> SourceData:
    rng = np.random.default_rng(7)
    features = rng.normal(size=(180, 29))
    labels = np.tile(np.array([0] * 19 + [1]), 9).astype(np.int8)
    splits = np.repeat(np.array(["train", "val", "test"]), 60)
    return SourceData(features, labels, splits, np.arange(180), "a" * 64)


def test_threshold_and_ties():
    assert threshold_for_validation([0, 0, 0, 1]) == pytest.approx(0.985)
    threshold = threshold_for_validation([0, 0, 1, 1])
    assert threshold == 1
    metrics = split_metrics(np.array([0, 0, 1, 1]), np.array([0, 0, 1, 1]), threshold)
    assert metrics["alert_rate"] == 0.5
    assert metrics["alert_count"] == 2
    assert (metrics["tp"], metrics["fp"], metrics["tn"], metrics["fn"]) == (2, 0, 2, 0)
    assert metrics["average_precision"] == 1
    assert metrics["roc_auc"] == 1


def test_fit_ignores_test_features_and_threshold_ignores_labels():
    source = sample_data()
    first = evaluate(source)
    altered = sample_data()
    altered.features[altered.splits == "test"] += 1000
    second = evaluate(altered)
    probe = source.features[:5]
    assert np.array_equal(first.model.decision_function(probe), second.model.decision_function(probe))
    assert first.threshold == second.threshold
    assert np.array_equal(first.validation_scores, -first.model.decision_function(source.features[60:120]))
    assert np.array_equal(first.validation_scores, second.validation_scores)
    source.labels[60:120] = 1 - source.labels[60:120]
    third = evaluate(source)
    assert first.threshold == third.threshold
    source.labels[:60] = 1 - source.labels[:60]
    fourth = evaluate(source)
    assert np.array_equal(first.model.decision_function(probe), fourth.model.decision_function(probe))


def test_metrics_require_both_classes():
    with pytest.raises(ValueError, match="both classes"):
        split_metrics(np.zeros(3), np.arange(3), 1.0)
