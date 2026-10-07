"""In-process registry of one API instance's WebSocket clients (spec 5.2).

Each client has a BOUNDED queue and its own sender task. `broadcast` is
non-blocking: it only does `put_nowait`. A client whose queue is full is a
slow consumer: it is disconnected (close 1013) and removed, so it can never
stall fan-out to the others. It repairs via REST on reconnect.
"""
import asyncio
import logging

log = logging.getLogger(__name__)

DEFAULT_QUEUE_MAX = 100
SLOW_CONSUMER_CODE = 1013
SLOW_CONSUMER_REASON = "slow consumer; resync via REST"
CLOSE_TIMEOUT = 1.0


class Client:
    def __init__(self, websocket, maxsize: int):
        self.websocket = websocket
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.task: asyncio.Task | None = None
        self.closed = False


class ConnectionManager:
    def __init__(self, queue_max: int = DEFAULT_QUEUE_MAX):
        self.queue_max = queue_max
        self._clients: set[Client] = set()
        self._closers: set[asyncio.Task] = set()

    @property
    def count(self) -> int:
        return len(self._clients)

    async def _sender(self, client: Client) -> None:
        try:
            while True:
                message = await client.queue.get()
                await client.websocket.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - peer gone; drop the client
            log.debug("sender ended: %s", exc)
            self._clients.discard(client)

    def connect(self, websocket) -> Client:
        """Register an ACCEPTED websocket and start its sender task."""
        client = Client(websocket, self.queue_max)
        client.task = asyncio.get_running_loop().create_task(self._sender(client))
        self._clients.add(client)
        return client

    async def disconnect(self, client: Client, code: int | None = None, reason: str = "") -> None:
        """Deregister, cancel the sender, optionally close the socket. Idempotent."""
        self._clients.discard(client)
        task, client.task = client.task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - cancelled or failed; either way it's over
                pass
        if code is not None and not client.closed:
            client.closed = True
            try:
                await asyncio.wait_for(client.websocket.close(code=code, reason=reason), CLOSE_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass

    def send(self, client: Client, message: dict) -> bool:
        """Non-blocking enqueue to one client; False if its queue is full."""
        try:
            client.queue.put_nowait(message)
            return True
        except asyncio.QueueFull:
            return False

    def _evict(self, client: Client) -> None:
        log.warning("slow consumer disconnected (queue full)")
        self._clients.discard(client)  # out of the registry immediately
        task = asyncio.get_running_loop().create_task(
            self.disconnect(client, SLOW_CONSUMER_CODE, SLOW_CONSUMER_REASON)
        )
        self._closers.add(task)  # keep a strong ref until done
        task.add_done_callback(self._closers.discard)

    def broadcast(self, message: dict) -> None:
        """Non-blocking fan-out; overflowing clients are evicted in the background."""
        for client in list(self._clients):
            if not self.send(client, message):
                self._evict(client)

    def broadcast_resync(self) -> None:
        self.broadcast({"type": "resync"})

    async def close_all(self) -> None:
        clients = list(self._clients)
        await asyncio.gather(
            *(self.disconnect(c, 1001, "server shutting down") for c in clients),
            *list(self._closers),
            return_exceptions=True,
        )
