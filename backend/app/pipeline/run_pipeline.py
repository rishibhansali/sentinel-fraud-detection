"""Standalone entry point: runs ReplayHarness against the live `transactions`
table, scoring each replayed transaction through the frozen detection engine
and persisting flagged cases. Long-lived process for local dev -- stop with
Ctrl+C (SIGINT), not a one-shot script.
"""
import argparse
import signal

import psycopg2

from app.pipeline.loader import DEFAULT_ROW_CAP
from app.pipeline.pipeline import make_pipeline_callback
from app.pipeline.replay import ReplayHarness
from app.pipeline.rules_provider import RulesProvider

DEFAULT_DSN = "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay transactions through the detection pipeline.")
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--speed-multiplier", type=float, default=1.0)
    parser.add_argument("--start-id", type=int, default=0)
    parser.add_argument("--end-id", type=int, default=None)
    parser.add_argument("--row-cap", type=int, default=DEFAULT_ROW_CAP)
    parser.add_argument("--rules-poll-seconds", type=float, default=30.0,
                        help="safety-net poll interval for rules hot reload")
    args = parser.parse_args()

    rules = RulesProvider(args.dsn)
    rules.start_hot_reload(poll_interval=args.rules_poll_seconds)
    processing_conn = psycopg2.connect(args.dsn)
    on_transaction = make_pipeline_callback(processing_conn, rules, row_cap=args.row_cap)

    harness = ReplayHarness(
        conn_factory=lambda: psycopg2.connect(args.dsn),
        speed_multiplier=args.speed_multiplier,
        start_id=args.start_id,
        end_id=args.end_id,
    )

    def handle_sigint(signum, frame):
        harness.stop()

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        harness.start(on_transaction)
        harness.join()
    finally:
        rules.stop_hot_reload()
        processing_conn.close()


if __name__ == "__main__":
    main()
