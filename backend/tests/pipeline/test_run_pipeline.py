"""End-to-end proof that the actual `run_pipeline.py` CLI artifact works --
not the pieces assembled by hand in test code, which is what every other
end-to-end test in this phase does (Task 3's test_pipeline.py builds
ReplayHarness + make_pipeline_callback inline; so does every LoadGenerator
test). Nothing before this test has ever invoked run_pipeline.py itself, so a
broken import, a wrong argparse flag, or a mis-wired conn_factory in that
file specifically would have gone uncaught by all 56 prior tests.

Runs it as a real subprocess (not an in-process main() call with a
monkeypatched sys.argv) -- the whole point is proving the artifact works as
an actual external caller would invoke it, which an in-process call can't
prove as faithfully. Accepting the subprocess-test fragility tradeoff
(path/venv sensitivity) deliberately, with a generous timeout.
"""
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest

from tests.pipeline.reserved_ranges import RUN_PIPELINE_CLI_TEST_ID_BASE, RUN_PIPELINE_CLI_TEST_USER_ID

DB_DSN = "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
BACKEND_DIR = Path(__file__).parent.parent.parent

TS = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
NYC = (40.7128, -74.0060)
LONDON = (51.5074, -0.1278)


def _insert_transaction(conn, id_: int, ts: datetime, lat: float, lon: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO transactions (
                id, user_id, card_id, ts, amount, lat, lon,
                v1, v2, v3, v4, v5, v6, v7, v8, v9, v10,
                v11, v12, v13, v14, v15, v16, v17, v18, v19, v20,
                v21, v22, v23, v24, v25, v26, v27, v28,
                class, split
            ) VALUES (
                %s, %s, 1, %s, 10.0, %s, %s,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                0, 0, 0, 0, 0, 0, 0, 0,
                0, 'test'
            );
            """,
            (id_, RUN_PIPELINE_CLI_TEST_USER_ID, ts, lat, lon),
        )
    conn.commit()


@pytest.fixture
def seeded_conn():
    conn = psycopg2.connect(DB_DSN)
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM flagged_cases WHERE transaction_id >= %s;", (RUN_PIPELINE_CLI_TEST_ID_BASE,))
            cur.execute("DELETE FROM transactions WHERE id >= %s;", (RUN_PIPELINE_CLI_TEST_ID_BASE,))
        conn.commit()
        conn.close()


def test_run_pipeline_cli_flags_correct_transaction(seeded_conn):
    prior_id = RUN_PIPELINE_CLI_TEST_ID_BASE + 1
    current_id = RUN_PIPELINE_CLI_TEST_ID_BASE + 2

    # Single deterministic geo-impossibility trip: prior in NYC, current in
    # London 60s later. Enough to prove the CLI wires correctly end to end --
    # scoring correctness itself is already exhaustively proven by Task 3's
    # test, this isn't re-litigating that.
    _insert_transaction(seeded_conn, prior_id, TS, NYC[0], NYC[1])
    _insert_transaction(seeded_conn, current_id, TS + timedelta(seconds=60), LONDON[0], LONDON[1])

    result = subprocess.run(
        [
            sys.executable, "-m", "app.pipeline.run_pipeline",
            "--dsn", DB_DSN,
            "--start-id", str(RUN_PIPELINE_CLI_TEST_ID_BASE),
            "--end-id", str(current_id),
            "--speed-multiplier", "10000",
        ],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, f"run_pipeline.py exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"

    with seeded_conn.cursor() as cur:
        cur.execute(
            "SELECT transaction_id, total_score, rule_results FROM flagged_cases WHERE transaction_id >= %s;",
            (RUN_PIPELINE_CLI_TEST_ID_BASE,),
        )
        rows = cur.fetchall()

    assert [r[0] for r in rows] == [current_id]

    _, total_score, rule_results = rows[0]
    by_rule = {r["rule_name"]: r for r in rule_results}
    assert by_rule["geo_impossibility"]["fired"] is True
    assert by_rule["velocity"]["fired"] is False
    assert by_rule["amount_baseline"]["fired"] is False
    assert total_score > 0
