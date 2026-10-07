"""Standalone entry point: runs ReplayHarness against the live `transactions`
table, scoring each replayed transaction through the frozen detection engine
and persisting flagged cases. Long-lived process for local dev -- stop with
Ctrl+C (SIGINT), not a one-shot script.
"""
import argparse
import signal

import psycopg2

from app.detection.config import load_rules_config
from app.pipeline.loader import DEFAULT_ROW_CAP
from app.pipeline.pipeline import make_pipeline_callback
from app.pipeline.replay import ReplayHarness

DEFAULT_DSN = "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay transactions through the detection pipeline.")
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--speed-multiplier", type=float, default=1.0)
    parser.add_argument("--start-id", type=int, default=0)
    parser.add_argument("--end-id", type=int, default=None)
    parser.add_argument("--row-cap", type=int, default=DEFAULT_ROW_CAP)
    args = parser.parse_args()

    rules_config = load_rules_config(args.dsn)
    processing_conn = psycopg2.connect(args.dsn)
    on_transaction = make_pipeline_callback(processing_conn, rules_config, row_cap=args.row_cap)

    harness = ReplayHarness(
        conn_factory=lambda: psycopg2.connect(args.dsn),
        speed_multiplier=args.speed_multiplier,
        start_id=args.start_id,
        end_id=args.end_id,
    )

    def handle_sigint(signum, frame):
        harness.stop()

    signal.signal(signal.SIGINT, handle_sigint)

    harness.start(on_transaction)
    harness.join()
    processing_conn.close()


if __name__ == "__main__":
    main()
