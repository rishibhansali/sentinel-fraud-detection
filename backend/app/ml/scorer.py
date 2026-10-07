"""Load a trusted Phase 6 artifact and score the shared transaction features."""

import hashlib
import json
import math
import re
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from app.ml.features import FEATURE_NAMES, extract_features

EXPECTED_MODEL_SETTINGS = {
    "n_estimators": 200,
    "max_samples": 256,
    "contamination": "auto",
    "random_state": 42,
    "n_jobs": 1,
}
FEATURE_TRANSFORM = "V1-V28 unchanged; log_amount=log1p(Amount)"
SCORE_FORMULA = "-model.decision_function(features)"
SCORE_ORIENTATION = "higher_is_more_anomalous"
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class AnomalyScorer:
    def __init__(self, model: IsolationForest, model_sha256: str):
        self._model = model
        self.model_sha256 = model_sha256

    def score_values(self, v_values: Sequence[object], amount: object) -> float:
        features = extract_features(v_values, amount)
        score = -float(self._model.decision_function(np.asarray([features], dtype=np.float64))[0])
        if not math.isfinite(score):
            raise ValueError("anomaly model returned a nonfinite score")
        return score


def _invalid(reason: str) -> ValueError:
    return ValueError(f"invalid anomaly artifact: {reason}")


def _manifest(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid("manifest.json is missing or malformed") from exc
    if not isinstance(value, dict):
        raise _invalid("manifest must be a JSON object")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise _invalid("model.joblib is missing or unreadable") from exc
    return digest.hexdigest()


def _validate_manifest(manifest: dict, model_path: Path) -> str:
    if manifest.get("feature_names") != list(FEATURE_NAMES):
        raise _invalid("feature names or order do not match the shared contract")
    if manifest.get("feature_transform") != FEATURE_TRANSFORM:
        raise _invalid("feature transform is incompatible")
    if manifest.get("model") != "IsolationForest" or manifest.get("model_settings") != EXPECTED_MODEL_SETTINGS:
        raise _invalid("model type or settings are incompatible")
    if manifest.get("score_formula") != SCORE_FORMULA or manifest.get("score_orientation") != SCORE_ORIENTATION:
        raise _invalid("score orientation is incompatible")
    threshold = manifest.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
        raise _invalid("threshold must be finite")
    expected_hash = manifest.get("model_sha256")
    if not isinstance(expected_hash, str) or _HASH_RE.fullmatch(expected_hash) is None:
        raise _invalid("model hash is malformed")
    actual_hash = _file_sha256(model_path)
    if actual_hash != expected_hash:
        raise _invalid("model hash does not match model.joblib")
    versions = manifest.get("dependency_versions")
    if not isinstance(versions, dict):
        raise _invalid("dependency versions are missing")
    python_version = versions.get("python")
    if not isinstance(python_version, str) or python_version.split(".")[:2] != [
        str(sys.version_info.major), str(sys.version_info.minor)
    ]:
        raise _invalid("Python version is incompatible")
    for key, package in (
        ("numpy", "numpy"), ("scipy", "scipy"),
        ("scikit_learn", "scikit-learn"), ("joblib", "joblib"),
    ):
        if versions.get(key) != version(package):
            raise _invalid(f"{package} version is incompatible")
    return actual_hash


def load_artifact(run_dir: Path) -> AnomalyScorer:
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise _invalid(f"directory does not exist: {run_dir}")
    model_path = run_dir / "model.joblib"
    model_sha256 = _validate_manifest(_manifest(run_dir / "manifest.json"), model_path)
    try:
        model = joblib.load(model_path)
    except Exception as exc:  # a corrupt, locally trusted artifact fails startup
        raise _invalid("model.joblib cannot be loaded") from exc
    if type(model) is not IsolationForest:
        raise _invalid("loaded model type is not IsolationForest")
    if getattr(model, "n_features_in_", None) != len(FEATURE_NAMES) or not hasattr(model, "estimators_"):
        raise _invalid("loaded model is not fitted with 29 features")
    params = model.get_params()
    if any(params.get(name) != expected for name, expected in EXPECTED_MODEL_SETTINGS.items()):
        raise _invalid("loaded model settings differ from manifest contract")
    return AnomalyScorer(model, model_sha256)
