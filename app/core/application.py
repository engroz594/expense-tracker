from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.database import engine
from app.core.errors import register_exception_handlers
from app.core.redis import redis_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Nothing to open at startup: the engine and Redis client connect lazily on first use.
    yield
    # On shutdown (e.g. SIGTERM during a rolling deploy) close pooled connections
    # cleanly instead of letting Postgres/Redis see them drop.
    await redis_client.aclose()
    await engine.dispose()


def create_application() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    from app.controllers.health_controller import router as health_router
    from routes.api.v1 import router as api_v1_router

    app.include_router(health_router)
    app.include_router(api_v1_router, prefix="/api/v1")
