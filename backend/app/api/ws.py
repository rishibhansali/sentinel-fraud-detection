"""WS /ws/cases: real-time case push (spec 5.2-5.3).

Pushes are HINTS. Redis pub/sub is at-most-once, so a push can be lost
(Redis gap, slow-consumer disconnect, reconnect). REST is the source of
truth.

Client protocol (spec 5.3):
  1. Connect the WebSocket FIRST. The server sends {"type": "hello"}.
  2. Then page GET /cases?since_id=0 to reconcile ALL case summaries,
     including updates to ids already seen. Apply each summary by replacing
     the cached value for its id; buffer concurrent pushes until paging ends.
  3. After a resync hint or reconnect, repeat the full scan. An incremental
     GET /cases?since_id=N (N = last case id seen) only finds new cases.
Server messages, forwarded verbatim from Redis:
  {"type": "case.created"|"case.updated", "case": {...}}
plus {"type": "hello"}, an application-level {"type": "heartbeat"} every
heartbeat interval (default 15s), and {"type": "resync"}, meaning pushes may
have been missed (Redis reconnected): repeat the full scan. A slow client is
closed with 1013; reconnect and repair. Periodic full reconciliation also
repairs a publish failure that does not disconnect Redis. Client text messages
are ignored.

LIMITATION: the since_id cursor (see `query_since_id` in app/api/cases.py)
assumes a SINGLE writer so commit order equals id order; with concurrent
writers a lower id can commit after a higher one and repair can skip it.
"""
import asyncio
import contextlib

from fastapi import APIRouter, WebSocket

router = APIRouter()

DEFAULT_HEARTBEAT_INTERVAL = 15.0


async def _heartbeat(manager, client, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        manager.send(client, {"type": "heartbeat"})  # full queue: next broadcast evicts it


@router.websocket("/ws/cases")
async def ws_cases(websocket: WebSocket):
    manager = websocket.app.state.ws_manager
    interval = websocket.app.state.ws_heartbeat_interval
    await websocket.accept()
    client = manager.connect(websocket)
    manager.send(client, {"type": "hello"})
    hb = asyncio.get_running_loop().create_task(_heartbeat(manager, client, interval))
    try:
        while True:
            await websocket.receive_text()  # content ignored; raises on disconnect
    except Exception:  # noqa: BLE001 - clean or abrupt disconnect, or non-text frame
        pass
    finally:
        hb.cancel()
        with contextlib.suppress(BaseException):
            await hb
        await manager.disconnect(client)
