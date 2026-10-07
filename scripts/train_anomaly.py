"""Train and report the fixed offline anomaly baseline; never touches the DB."""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import joblib
import numpy as np

from ml_dataset import FEATURE_NAMES, load_source_csv
from ml_evaluation import MODEL_SETTINGS, VALIDATION_QUANTILE, evaluate

ROOT = Path(__file__).resolve().parents[1]


def _revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
    ).strip()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _score_hash(scores: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(scores, dtype="<f8").tobytes()).hexdigest()


def _report(manifest: dict, metrics: dict) -> str:
    rows = [
        "# Offline anomaly baseline",
        "",
        f"Source SHA-256: `{manifest['source_sha256']}` ({manifest['source_row_count']:,} original rows).",
        f"Code revision: `{manifest['code_revision']}`.",
        "",
        "The original source rows were stratified once by Class with seed 42 before any synthetic replication. "
        "Their zero-based source indices remain disjoint across train, validation, and test.",
        "The model was fit on all training rows without labels. Validation labels and test rows did not set the threshold.",
        "",
        f"Split counts: train {manifest['split_counts']['train']:,}, validation "
        f"{manifest['split_counts']['val']:,}, test {manifest['split_counts']['test']:,}.",
        f"Anomaly score is -decision_function (higher means more anomalous, not a probability). "
        f"The fixed >= threshold is {manifest['threshold']:.17g}, the 99.5th percentile of validation scores.",
        "",
        "| Split | Rows | Prevalence (no-skill AP) | AP | ROC-AUC | Precision | Recall | Alerts | Alert rate | TP | FP | TN | FN |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("validation", "test"):
        item = metrics[name]
        rows.append(
            f"| {name} | {item['sample_count']} | {item['prevalence']:.6f} | "
            f"{item['average_precision']:.6f} | {item['roc_auc']:.6f} | "
            f"{item['precision']:.6f} | {item['recall']:.6f} | "
            f"{item['alert_count']} | {item['alert_rate']:.6f} | "
            f"{item['tp']} | {item['fp']} | {item['tn']} | {item['fn']} |"
        )
    rows.extend([
        "",
        "## Limits",
        "",
        "This is an offline random holdout, not a prospective time-based evaluation or a production fraud guarantee. "
        "The original V1–V28 fields are PCA-derived and their fitting provenance is unavailable. "
        "A future model comparison needs a new independent holdout; this test split cannot be reused for tuning. "
        "The observed result does not authorize live scoring or change deterministic case creation.",
        "",
    ])
    return "\n".join(rows)


def train_and_write(
    source: Path,
    output_root: Path,
    expected_rows: int = 284_807,
    run_id: str | None = None,
) -> Path:
    data = load_source_csv(source, expected_rows=expected_rows)
    result = evaluate(data)
    revision = _revision()
    if run_id is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_id = f"{timestamp}-{data.source_sha256[:12]}-{revision[:12]}"
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("run_id must be a single directory name")
    output_root = Path(output_root)
    run_dir = output_root / run_id
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(exist_ok=False)
    model_path = run_dir / "model.joblib"
    joblib.dump(result.model, model_path)
    manifest = {
        "source_sha256": data.source_sha256,
        "source_row_count": len(data.labels),
        "source_row_index": "zero-based original CSV order",
        "split_method": "assign_splits before replication, stratified by Class",
        "split_seed": 42,
        "split_counts": {name: int(np.count_nonzero(data.splits == name)) for name in ("train", "val", "test")},
        "feature_names": list(FEATURE_NAMES),
        "feature_transform": "V1-V28 unchanged; log_amount=log1p(Amount)",
        "model": "IsolationForest",
        "model_settings": MODEL_SETTINGS,
        "score_formula": "-model.decision_function(features)",
        "score_orientation": "higher_is_more_anomalous",
        "validation_quantile": VALIDATION_QUANTILE,
        "quantile_method": "linear",
        "alert_comparison": ">=",
        "threshold": result.threshold,
        "validation_score_sha256": _score_hash(result.validation_scores),
        "test_score_sha256": _score_hash(result.test_scores),
        "model_sha256": _hash_file(model_path),
        "dependency_versions": {
            "python": sys.version.split()[0],
            "numpy": version("numpy"),
            "pandas": version("pandas"),
            "scikit_learn": version("scikit-learn"),
            "joblib": version("joblib"),
        },
        "code_revision": revision,
    }
    metrics = {"threshold": result.threshold, **result.metrics}
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    (run_dir / "report.md").write_text(_report(manifest, result.metrics))
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the fixed anomaly baseline on original ULB rows")
    parser.add_argument("--source", type=Path, default=ROOT / "data/raw/creditcard.csv")
    parser.add_argument("--output-root", type=Path, default=ROOT / "artifacts/ml")
    args = parser.parse_args()
    try:
        run_dir = train_and_write(args.source, args.output_root)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
