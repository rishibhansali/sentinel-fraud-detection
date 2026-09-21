"""Redis connection settings and channel names for the real-time layer."""
import os

CASES_CHANNEL = "cases:events"
RULES_CHANNEL = "rules:updated"

DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def redis_url() -> str:
    return os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)
