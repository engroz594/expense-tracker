"""Liveness and readiness probes (mounted at the root, outside /api/v1).

/health — liveness: is the process up? Checks nothing else, so a database outage
          doesn't make the orchestrator kill and restart healthy API containers.
/ready  — readiness: can this instance serve traffic? Pings Postgres and Redis.
          The load balancer stops routing to an instance that returns 503 here.
"""

import asyncio
import logging
from collections.abc import Awaitable

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.exceptions import ServiceUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, object]:
    checks = {
        "database": await _probe("database", db.execute(text("SELECT 1"))),
        "redis": await _probe("redis", redis.ping()),
    }
    if not all(checks.values()):
        raise ServiceUnavailable(
            "Dependency check failed",
            details=[{"check": name, "ok": ok} for name, ok in checks.items()],
        )
    return {"status": "ok", "checks": {name: "ok" for name in checks}}


async def _probe(name: str, check: Awaitable[object]) -> bool:
    try:
        async with asyncio.timeout(settings.READINESS_TIMEOUT_SECONDS):
            await check
        return True
    except Exception:
        logger.warning("Readiness check '%s' failed", name, exc_info=True)
        return False
