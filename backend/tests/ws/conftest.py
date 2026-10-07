import os
import threading
import time

import psycopg2
import pytest
import uvicorn

from app.main import create_app
from tests.pipeline.reserved_ranges import WS_TEST_TXN_BASE, WS_TEST_TXN_MAX

DB_DSN = os.environ.get(
    "SENTINEL_DB_DSN", "postgresql://sentinel:sentinel_dev_only@localhost:5432/sentinel"
)
TIMEOUT = 5.0


class LiveServer:
    """A real uvicorn server on an ephemeral port in a background thread."""

    def __init__(self, app):
        self.app = app
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self, wait_subscribed: bool = True):
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            assert time.time() < deadline and self.thread.is_alive(), "server failed to start"
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        if wait_subscribed:
            self.wait_subscribed()
        return self

    def wait_subscribed(self, n: int = 1):
        sub = self.app.state.case_subscriber
        deadline = time.time() + 10
        while sub._subscriptions < n:
            assert time.time() < deadline, "subscriber never subscribed"
            time.sleep(0.02)

    @property
    def manager(self):
        return self.app.state.ws_manager

    @property
    def ws_url(self):
        return f"ws://127.0.0.1:{self.port}/ws/cases"

    @property
    def http_url(self):
        return f"http://127.0.0.1:{self.port}"

    def wait_clients(self, n: int, timeout: float = 5.0):
        deadline = time.time() + timeout
        while self.manager.count != n:
            assert time.time() < deadline, f"client count {self.manager.count} != {n}"
            time.sleep(0.02)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(15)
        assert not self.thread.is_alive(), "server did not shut down"


@pytest.fixture
def live_server_factory():
    servers = []

    def make(wait_subscribed=True, **kwargs):
        s = LiveServer(create_app(**kwargs)).start(wait_subscribed)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.stop()


@pytest.fixture
def live_server(live_server_factory):
    return live_server_factory()


def cleanup(c):
    with c.cursor() as cur:
        cur.execute(
            "DELETE FROM case_feedback WHERE case_id IN "
            "(SELECT id FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s)",
            (WS_TEST_TXN_BASE, WS_TEST_TXN_MAX),
        )
        cur.execute(
            "DELETE FROM flagged_cases WHERE transaction_id BETWEEN %s AND %s",
            (WS_TEST_TXN_BASE, WS_TEST_TXN_MAX),
        )
    c.commit()


@pytest.fixture
def conn():
    c = psycopg2.connect(DB_DSN)
    cleanup(c)
    yield c
    c.rollback()
    cleanup(c)
    c.close()
