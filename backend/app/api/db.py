"""psycopg2 ThreadedConnectionPool, created lazily so it works with or without
the FastAPI lifespan. `get_conn` is the FastAPI dependency."""
import os
import threading

from psycopg2.pool import ThreadedConnectionPool

DEFAULT_DSN = "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
POOL_MIN = 1
POOL_MAX = 10

_lock = threading.Lock()
_pool: ThreadedConnectionPool | None = None


def dsn() -> str:
    return os.environ.get("SENTINEL_DB_DSN", DEFAULT_DSN)


def get_pool() -> ThreadedConnectionPool:
    global _pool
    with _lock:
        if _pool is None or _pool.closed:
            _pool = ThreadedConnectionPool(POOL_MIN, POOL_MAX, dsn())
        return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


def get_conn():
    """Borrow a connection; ALWAYS return it (after rolling back any open tx)."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            pool.putconn(conn)
