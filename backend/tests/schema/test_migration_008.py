"""Phase 8 summary columns remain optional on ordinary rule-created cases."""
import os

import psycopg2


DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)


def test_summary_columns_are_nullable_and_have_no_default():
    with psycopg2.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'flagged_cases' AND column_name = ANY(%s)",
                (["ai_summary", "ai_summary_model", "ai_summary_generated_at"],),
            )
            cols = {name: (nullable, default) for name, nullable, default in cur.fetchall()}
    assert cols == {name: ("YES", None) for name in (
        "ai_summary", "ai_summary_model", "ai_summary_generated_at"
    )}
