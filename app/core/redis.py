from redis.asyncio import Redis

from app.core.config import settings

# Shared async client; it manages its own connection pool. decode_responses=True
# returns str instead of bytes, which is what the rate limiter and idempotency code want.
redis_client: Redis = Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    password=settings.REDIS_PASSWORD.get_secret_value() if settings.REDIS_PASSWORD else None,
    decode_responses=True,
)


async def get_redis() -> Redis:
    """FastAPI dependency, so tests can override the client if needed."""
    return redis_client
