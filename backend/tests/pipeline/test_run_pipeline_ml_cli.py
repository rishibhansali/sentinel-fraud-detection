"""The opt-in CLI must reject invalid artifacts before replay starts."""

import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]


def test_help_exposes_explicit_model_directory():
    result = subprocess.run(
        [sys.executable, "-m", "app.pipeline.run_pipeline", "--help"],
        cwd=BACKEND_DIR, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert "--ml-artifact-dir" in result.stdout


def test_invalid_artifact_stops_before_replay(tmp_path):
    result = subprocess.run(
        [
            sys.executable, "-m", "app.pipeline.run_pipeline",
            "--ml-artifact-dir", str(tmp_path / "missing"),
            "--start-id", "281000000", "--end-id", "281000001",
        ],
        cwd=BACKEND_DIR, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "invalid anomaly artifact" in result.stderr
    assert "directory does not exist" in result.stderr
