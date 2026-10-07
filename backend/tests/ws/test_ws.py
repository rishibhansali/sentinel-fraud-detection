"""Real uvicorn servers, real websockets client, real Redis + Postgres."""
import asyncio
import json
import socket

import httpx
import redis
import websockets

from app.realtime import publisher
from app.realtime.config import redis_url
from tests.pipeline.reserved_ranges import WS_TEST_TXN_BASE, WS_TEST_USER_ID
from tests.ws.conftest import TIMEOUT


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


async def recv(ws, timeout=TIMEOUT):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


def summary(cid, **extra):
    return {"id": cid, "status": "open", **extra}


async def wait_clients(srv, n):
    await asyncio.get_running_loop().run_in_executor(None, srv.wait_clients, n)


def test_hello_then_push_verbatim(live_server):
    async def go():
        async with websockets.connect(live_server.ws_url) as ws:
            assert await recv(ws) == {"type": "hello"}
            assert publisher.publish_case_event("case.created", summary(1))
            assert await recv(ws) == {"type": "case.created", "case": summary(1)}
            publisher.publish_case_event("case.updated", summary(1, status="in_review"))
            assert await recv(ws) == {"type": "case.updated", "case": summary(1, status="in_review")}

    run(go())


def test_multi_instance_fanout_via_redis(live_server_factory):
    a, b = live_server_factory(), live_server_factory()
    assert a.port != b.port and a.app is not b.app

    async def go():
        async with websockets.connect(a.ws_url) as wa, websockets.connect(b.ws_url) as wb:
            assert (await recv(wa))["type"] == "hello"
            assert (await recv(wb))["type"] == "hello"
            publisher.publish_case_event("case.created", summary(42))
            expect = {"type": "case.created", "case": summary(42)}
            assert await recv(wa) == expect
            assert await recv(wb) == expect

    run(go())


def test_many_clients_and_deregistration(live_server):
    async def go():
        conns = [await websockets.connect(live_server.ws_url) for _ in range(3)]
        for c in conns:
            await recv(c)
        publisher.publish_case_event("case.created", summary(7))
        for c in conns:
            assert (await recv(c))["case"]["id"] == 7
        assert live_server.manager.count == 3
        await conns[0].close()  # clean
        conns[1].transport.abort()  # abrupt, no close handshake
        await wait_clients(live_server, 1)
        await conns[2].close()
        await wait_clients(live_server, 0)

    run(go())


def test_client_text_messages_ignored(live_server):
    async def go():
        async with websockets.connect(live_server.ws_url) as ws:
            await recv(ws)
            await ws.send("hi there")
            await ws.send(json.dumps({"x": 1}))
            publisher.publish_case_event("case.created", summary(9))
            assert (await recv(ws))["case"]["id"] == 9

    run(go())


def test_real_socket_non_reading_client_does_not_block_others(live_server_factory):
    srv = live_server_factory(ws_queue_max=5)
    pad = "x" * 1_000_000

    async def go():
        # Raw socket: completes the handshake, never reads; tiny kernel receive buffer so the server's socket backs up.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.connect(("127.0.0.1", srv.port))
        sock.sendall(
            b"GET /ws/cases HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        async with websockets.connect(srv.ws_url, max_size=None) as good:
            await recv(good)
            await wait_clients(srv, 2)
            n = 50
            for i in range(n):
                publisher.publish_case_event("case.created", summary(i, pad=pad))
                await asyncio.sleep(0.005)
            for i in range(n):
                assert (await recv(good, 10))["case"]["id"] == i
            await wait_clients(srv, 1)  # the stuck one was cut off
        sock.close()

    run(go())


def test_heartbeat(live_server_factory):
    srv = live_server_factory(ws_heartbeat_interval=0.1)

    async def go():
        async with websockets.connect(srv.ws_url) as ws:
            assert (await recv(ws))["type"] == "hello"
            assert await recv(ws, 2) == {"type": "heartbeat"}
            assert await recv(ws, 2) == {"type": "heartbeat"}

    run(go())


def test_resync_after_redis_pubsub_kill(live_server):
    async def go():
        async with websockets.connect(live_server.ws_url) as ws:
            await recv(ws)
            r = redis.Redis.from_url(redis_url())
            try:
                assert r.execute_command("CLIENT", "KILL", "TYPE", "pubsub") >= 1
            finally:
                r.close()
            while await recv(ws, 10) != {"type": "resync"}:
                pass
            # resync fires once the subscription is re-established
            publisher.publish_case_event("case.created", summary(77))
            while True:
                m = await recv(ws, 10)
                if m.get("type") == "case.created":
                    assert m["case"]["id"] == 77
                    break

    run(go())


def _insert_case(conn, n):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO flagged_cases
               (transaction_id, transaction_ts, user_id, total_score, rule_results,
                priority_score, status)
               VALUES (%s, '2025-01-01T00:00:00Z', %s, 0.5, '[]', 1e9, 'open') RETURNING id""",
            (WS_TEST_TXN_BASE + n, WS_TEST_USER_ID),
        )
        cid = cur.fetchone()[0]
    conn.commit()
    return cid


class RefClient:
    """Reference client: incremental creations, full reconciliation after a gap."""

    def __init__(self, srv, last_seen):
        self.srv, self.last_seen = srv, last_seen
        self.seen: dict[int, dict] = {}
        self.duplicates = 0

    def apply(self, case):
        if case["id"] in self.seen:
            self.duplicates += 1
        self.seen[case["id"]] = case
        self.last_seen = max(self.last_seen, case["id"])

    async def repair(self, http, full=False):
        cursor = 0 if full else self.last_seen
        while True:
            r = await http.get(f"{self.srv.http_url}/cases", params={"since_id": cursor})
            r.raise_for_status()
            body = r.json()
            for c in body["items"]:
                self.apply(c)
            if body["next_since_id"] is None:
                return
            cursor = body["next_since_id"]


def test_repair_protocol_end_to_end(live_server, conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(max(id), 0) FROM flagged_cases")
        last = cur.fetchone()[0]
    conn.commit()
    ref = RefClient(live_server, last)

    async def go():
        async with websockets.connect(live_server.ws_url) as ws, httpx.AsyncClient(timeout=5) as http:
            assert await recv(ws) == {"type": "hello"}  # 1. WS first
            missed = await asyncio.to_thread(_insert_case, conn, 1)  # push never published
            await ref.repair(http, full=True)  # 2. full reconciliation over REST
            assert missed in ref.seen
            # 3. a later push of the same case id updates the stored summary
            publisher.publish_case_event("case.updated", summary(missed, status="in_review"))
            msg = await recv(ws)
            ref.apply(msg["case"])
            assert ref.duplicates == 1
            assert ref.seen[missed]["status"] == "in_review"
            assert list(ref.seen).count(missed) == 1

    run(go())


def test_full_repair_recovers_missed_update(live_server, conn):
    cid = _insert_case(conn, 2)
    ref = RefClient(live_server, cid)

    async def go():
        async with websockets.connect(live_server.ws_url) as ws, httpx.AsyncClient(timeout=5) as http:
            assert await recv(ws) == {"type": "hello"}
            await ref.repair(http, full=True)
            assert ref.seen[cid]["status"] == "open"
            r = await http.post(f"{live_server.http_url}/cases/{cid}/claim", json={"analyst": "alice"})
            assert r.status_code == 200
            # Simulate a lost case.updated push by deliberately not applying it.
            await ref.repair(http, full=True)
            assert ref.seen[cid]["status"] == "in_review"

    run(go())


def test_redis_down_startup_and_shutdown(live_server_factory):
    srv = live_server_factory(wait_subscribed=False, redis_url_override="redis://localhost:6390/0")

    async def go():
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(f"{srv.http_url}/health")
            assert r.json() == {"status": "ok"}
        async with websockets.connect(srv.ws_url) as ws:
            assert await recv(ws) == {"type": "hello"}

    run(go())
    # fixture teardown asserts a clean shutdown


def test_first_subscription_after_startup_outage_signals_resync(live_server_factory):
    srv = live_server_factory(wait_subscribed=False, redis_url_override="redis://localhost:6390/0")

    async def go():
        async with websockets.connect(srv.ws_url) as ws:
            assert await recv(ws) == {"type": "hello"}
            srv.app.state.case_subscriber.url = redis_url()
            await asyncio.to_thread(srv.wait_subscribed)
            assert await recv(ws, 5) == {"type": "resync"}

    run(go())
