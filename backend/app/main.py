from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import db
from app.api.cases import router as cases_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup hooks (e.g. the Redis subscriber, Task 6) go before `yield`;
    # their teardown goes in the finally block. The DB pool is lazy, so
    # nothing to start for it.
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(title="Sentinel", lifespan=lifespan)
app.include_router(cases_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
