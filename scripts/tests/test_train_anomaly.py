import hashlib
import json
import subprocess
import sys
from pathlib import Path

import joblib
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from test_ml_dataset import make_source
from train_anomaly import train_and_write

ROOT = Path(__file__).resolve().parents[2]


def test_artifact_contains_reproducible_contract(tmp_path):
    source = tmp_path / "source.csv"
    make_source(source)
    root = tmp_path / "runs"
    first = train_and_write(source, root, expected_rows=400)
    assert first.parent == root
    assert {item.name for item in first.iterdir()} == {
        "model.joblib", "manifest.json", "metrics.json", "report.md"
    }
    manifest = json.loads((first / "manifest.json").read_text())
    metrics = json.loads((first / "metrics.json").read_text())
    assert manifest["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["source_row_count"] == 400
    assert sum(manifest["split_counts"].values()) == 400
    assert manifest["feature_names"] == [f"v{i}" for i in range(1, 29)] + ["log_amount"]
    assert manifest["model_settings"]["n_estimators"] == 200
    assert manifest["score_orientation"] == "higher_is_more_anomalous"
    assert manifest["threshold"] == metrics["threshold"]
    assert manifest["dependency_versions"]["scikit_learn"]
    assert manifest["code_revision"] == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    assert manifest["model_sha256"] == hashlib.sha256((first / "model.joblib").read_bytes()).hexdigest()
    assert joblib.load(first / "model.joblib").n_features_in_ == 29
    report = (first / "report.md").read_text()
    assert "validation" in report and "test" in report
    assert "time-based" in report and "PCA" in report
    second = train_and_write(source, root, expected_rows=400)
    assert second != first
    assert json.loads((second / "metrics.json").read_text()) == metrics


def test_collision_and_missing_source_leave_output_alone(tmp_path):
    source = tmp_path / "source.csv"
    make_source(source)
    root = tmp_path / "runs"
    first = train_and_write(source, root, expected_rows=400, run_id="fixed")
    before = {path.name: path.read_bytes() for path in first.iterdir()}
    with pytest.raises(FileExistsError):
        train_and_write(source, root, expected_rows=400, run_id="fixed")
    assert {path.name: path.read_bytes() for path in first.iterdir()} == before
    with pytest.raises(FileNotFoundError):
        train_and_write(tmp_path / "missing.csv", tmp_path / "empty", expected_rows=400)
    assert not (tmp_path / "empty").exists()


def test_cli_help_and_missing_source(tmp_path):
    script = ROOT / "scripts" / "train_anomaly.py"
    help_result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0
    assert "--source" in help_result.stdout
    output = tmp_path / "runs"
    missing = subprocess.run(
        [sys.executable, str(script), "--source", str(tmp_path / "missing.csv"), "--output-root", str(output)],
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    assert "data/README.md" in missing.stderr
    assert not output.exists()
