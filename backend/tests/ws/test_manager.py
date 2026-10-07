"""ConnectionManager unit tests with stub websockets (no network)."""
import asyncio
import time

from app.realtime.manager import ConnectionManager


class StubWS:
    def __init__(self, hang=False):
        self.hang = hang
        self.sent = []
        self.closed = None

    async def send_json(self, m):
        if self.hang:
            await asyncio.Event().wait()  # never completes
        self.sent.append(m)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def test_broadcast_delivers_and_counts():
    async def go():
        m = ConnectionManager(queue_max=10)
        a, b = StubWS(), StubWS()
        ca, cb = m.connect(a), m.connect(b)
        assert m.count == 2
        m.broadcast({"n": 1})
        m.broadcast_resync()
        await asyncio.sleep(0.05)
        assert a.sent == b.sent == [{"n": 1}, {"type": "resync"}]
        await m.disconnect(ca)
        assert m.count == 1
        await m.disconnect(ca)  # idempotent
        await m.close_all()
        assert m.count == 0 and b.closed[0] == 1001

    asyncio.run(go())


def test_slow_consumer_is_disconnected_without_blocking_others():
    async def go():
        m = ConnectionManager(queue_max=3)
        slow, fast = StubWS(hang=True), StubWS()
        m.connect(slow)
        m.connect(fast)
        worst = 0.0
        for i in range(20):
            t = time.perf_counter()
            m.broadcast({"n": i})
            worst = max(worst, time.perf_counter() - t)
            await asyncio.sleep(0)  # let fast sender drain
        await asyncio.sleep(0.05)
        assert worst < 0.05, f"broadcast blocked for {worst:.3f}s"
        assert m.count == 1  # only the slow one was removed
        assert slow.closed == (1013, "slow consumer; resync via REST")
        assert fast.closed is None
        assert [x["n"] for x in fast.sent] == list(range(20))
        await m.close_all()

    asyncio.run(go())
