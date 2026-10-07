"""Phase 6 artifact compatibility and shared inference-feature tests."""

import copy
import hashlib
import json
import math
import sys
from importlib.metadata import version

import joblib
import numpy as np
import pytest
from sklearn.ensemble import IsolationForest

from app.ml.scorer import load_artifact


@pytest.fixture(scope="module")
def fitted_model():
    model = IsolationForest(
        n_estimators=200, max_samples=256, contamination="auto", random_state=42, n_jobs=1
    )
    model.fit(np.random.default_rng(7).normal(size=(300, 29)))
    return model


def write_artifact(path, model, manifest_changes=None):
    path.mkdir()
    model_path = path / "model.joblib"
    joblib.dump(model, model_path)
    manifest = {
        "feature_names": [f"v{i}" for i in range(1, 29)] + ["log_amount"],
        "feature_transform": "V1-V28 unchanged; log_amount=log1p(Amount)",
        "model": "IsolationForest",
        "model_settings": {
            "n_estimators": 200, "max_samples": 256, "contamination": "auto",
            "random_state": 42, "n_jobs": 1,
        },
        "score_formula": "-model.decision_function(features)",
        "score_orientation": "higher_is_more_anomalous",
        "threshold": 0.1,
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "dependency_versions": {
            "python": sys.version.split()[0],
            "numpy": version("numpy"),
            "scipy": version("scipy"),
            "scikit_learn": version("scikit-learn"),
            "joblib": version("joblib"),
        },
    }
    for key, value in (manifest_changes or {}).items():
        manifest[key] = value
    (path / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_compatible_artifact_scores_using_shared_order_and_sign(tmp_path, fitted_model):
    run = tmp_path / "run"
    manifest = write_artifact(run, fitted_model)
    scorer = load_artifact(run)
    expected_features = np.asarray([[*range(1, 29), math.log1p(99)]], dtype=float)
    expected = -fitted_model.decision_function(expected_features)[0]
    assert scorer.score_values(tuple(range(1, 29)), 99) == pytest.approx(expected)
    assert scorer.model_sha256 == manifest["model_sha256"]


@pytest.mark.parametrize(
    "change",
    [
        {"feature_names": ["v0"] + [f"v{i}" for i in range(2, 29)] + ["log_amount"]},
        {"feature_transform": "Amount unchanged"},
        {"model": "SomeOtherModel"},
        {"model_settings": {"n_estimators": 10}},
        {"score_formula": "model.decision_function(features)"},
        {"score_orientation": "lower_is_more_anomalous"},
        {"threshold": float("nan")},
        {"model_sha256": "0" * 64},
        {"dependency_versions": {"python": "2.7.0"}},
    ],
)
def test_incompatible_manifest_is_rejected(tmp_path, fitted_model, change):
    run = tmp_path / "run"
    write_artifact(run, fitted_model, copy.deepcopy(change))
    with pytest.raises(ValueError, match="artifact|manifest|version|hash|threshold"):
        load_artifact(run)


def test_missing_or_malformed_artifact_fails(tmp_path, fitted_model):
    with pytest.raises(ValueError, match="artifact"):
        load_artifact(tmp_path / "missing")
    run = tmp_path / "run"
    write_artifact(run, fitted_model)
    (run / "manifest.json").write_text("not json")
    with pytest.raises(ValueError, match="manifest"):
        load_artifact(run)


def test_model_must_be_fitted_and_correct_width(tmp_path):
    unfitted = IsolationForest(
        n_estimators=200, max_samples=256, contamination="auto", random_state=42, n_jobs=1
    )
    run = tmp_path / "unfitted"
    write_artifact(run, unfitted)
    with pytest.raises(ValueError, match="fitted|features"):
        load_artifact(run)
    wrong_width = IsolationForest(
        n_estimators=200, max_samples=256, contamination="auto", random_state=42, n_jobs=1
    )
    wrong_width.fit(np.random.default_rng(3).normal(size=(300, 28)))
    run2 = tmp_path / "wrong_width"
    write_artifact(run2, wrong_width)
    with pytest.raises(ValueError, match="features"):
        load_artifact(run2)
