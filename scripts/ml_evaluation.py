"""Fixed, label-free Isolation Forest fit and held-out evaluation."""

from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score, roc_auc_score

from ml_dataset import SourceData

MODEL_SETTINGS = {
    "n_estimators": 200,
    "max_samples": 256,
    "contamination": "auto",
    "random_state": 42,
    "n_jobs": 1,
}
VALIDATION_QUANTILE = 0.995


@dataclass(frozen=True)
class Evaluation:
    model: IsolationForest
    threshold: float
    metrics: dict[str, dict]
    validation_scores: np.ndarray
    test_scores: np.ndarray


def threshold_for_validation(scores: np.ndarray | list[float]) -> float:
    values = np.asarray(scores, dtype=np.float64)
    if values.ndim != 1 or len(values) == 0 or not np.isfinite(values).all():
        raise ValueError("Validation scores must be a nonempty finite vector")
    return float(np.quantile(values, VALIDATION_QUANTILE, method="linear"))


def split_metrics(labels: np.ndarray, scores: np.ndarray, threshold: float) -> dict:
    labels = np.asarray(labels)
    scores = np.asarray(scores, dtype=np.float64)
    if labels.ndim != 1 or scores.ndim != 1 or len(labels) != len(scores):
        raise ValueError("Labels and scores must be equal-length vectors")
    if not np.isin(labels, (0, 1)).all() or len(np.unique(labels)) != 2:
        raise ValueError("Evaluation split must contain both classes")
    if not np.isfinite(scores).all() or not np.isfinite(threshold):
        raise ValueError("Scores and threshold must be finite")
    alerts = scores >= threshold
    positive = labels == 1
    tp = int(np.count_nonzero(alerts & positive))
    fp = int(np.count_nonzero(alerts & ~positive))
    tn = int(np.count_nonzero(~alerts & ~positive))
    fn = int(np.count_nonzero(~alerts & positive))
    count = len(labels)
    alert_count = tp + fp
    return {
        "sample_count": count,
        "positive_count": int(np.count_nonzero(positive)),
        "prevalence": float(np.mean(positive)),
        "average_precision": float(average_precision_score(labels, scores)),
        "roc_auc": float(roc_auc_score(labels, scores)),
        "precision": tp / alert_count if alert_count else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
        "alert_count": alert_count,
        "alert_rate": alert_count / count,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
    }


def evaluate(data: SourceData) -> Evaluation:
    train = data.splits == "train"
    val = data.splits == "val"
    test = data.splits == "test"
    if not train.any() or not val.any() or not test.any():
        raise ValueError("Train, validation, and test splits must all be nonempty")
    model = IsolationForest(**MODEL_SETTINGS)
    model.fit(data.features[train])
    validation_scores = -model.decision_function(data.features[val])
    threshold = threshold_for_validation(validation_scores)
    test_scores = -model.decision_function(data.features[test])
    metrics = {
        "validation": split_metrics(data.labels[val], validation_scores, threshold),
        "test": split_metrics(data.labels[test], test_scores, threshold),
    }
    return Evaluation(model, threshold, metrics, validation_scores, test_scores)
