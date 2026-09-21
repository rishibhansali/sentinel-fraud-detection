import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import db
from app.api.cases import router as cases_router
from app.api.rules import router as rules_router
from app.api.ws import DEFAULT_HEARTBEAT_INTERVAL, router as ws_router
from app.realtime.config import CASES_CHANNEL, redis_url
from app.realtime.manager import DEFAULT_QUEUE_MAX, ConnectionManager
from app.realtime.subscriber import AsyncChannelSubscriber


def create_app(
    redis_url_override: str | None = None,
    ws_queue_max: int = DEFAULT_QUEUE_MAX,
    ws_heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Add further startup hooks (Task 7) before `yield` and their teardown
        # in the finally block. The DB pool is lazy, nothing to start for it.
        manager = ConnectionManager(queue_max=ws_queue_max)
        app.state.ws_manager = manager
        app.state.ws_heartbeat_interval = ws_heartbeat_interval
        # ONE subscription per API instance. Startup never blocks or fails if
        # Redis is down: the subscriber reconnects with backoff.
        subscriber = AsyncChannelSubscriber(
            redis_url_override or redis_url(),
            CASES_CHANNEL,
            on_message=manager.broadcast,
            on_reconnect=manager.broadcast_resync,
        )
        app.state.case_subscriber = subscriber
        subscriber_task = asyncio.get_running_loop().create_task(subscriber.run())
        try:
            yield
        finally:
            subscriber.stop()
            try:
                await asyncio.wait_for(asyncio.shield(subscriber_task), 3)
            except BaseException:  # noqa: BLE001 - timeout: force it
                subscriber_task.cancel()
                with contextlib.suppress(BaseException):
                    await subscriber_task
            try:
                await manager.close_all()
            finally:
                db.close_pool()

    app = FastAPI(title="Sentinel", lifespan=lifespan)
    app.include_router(cases_router)
    app.include_router(rules_router)
    app.include_router(ws_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
