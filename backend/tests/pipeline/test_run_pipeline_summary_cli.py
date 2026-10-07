"""Summary opt-in must validate its credential before starting replay."""
import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]


def test_help_exposes_summary_opt_in():
    result = subprocess.run(
        [sys.executable, "-m", "app.pipeline.run_pipeline", "--help"],
        cwd=BACKEND_DIR, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0
    assert "--claude-summaries" in result.stdout


def test_missing_key_stops_before_replay():
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-m", "app.pipeline.run_pipeline", "--claude-summaries",
         "--start-id", "291000000", "--end-id", "291000001"],
        cwd=BACKEND_DIR, capture_output=True, text=True, timeout=15, env=env,
    )
    assert result.returncode != 0
    assert "ANTHROPIC_API_KEY" in result.stderr
