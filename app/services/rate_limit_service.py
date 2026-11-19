"""Login rate limit: at most 5 failed logins per email per 15 minutes, in Redis.

Built on the `limits` library's async API (limits.aio), using its Redis storage with
the redis-py asyncio client we already depend on (implementation="redispy") — so
no blocking calls and no second Redis client library. (slowapi wasn't used: it
calls the synchronous limits API, can't key on a JSON body field, and counts every
request rather than only failures.)

- Shared in Redis, so the limit holds across all API servers behind a load balancer.
- Moving window: at most 5 attempts in *any* 15-minute period; Retry-After is when
  the oldest counted attempt leaves the window.
- Attempts are counted *before* the password is checked, with `hit()`, which checks
  and records atomically — parallel guesses can't slip past the limit. A successful
  login clears the counter, so in effect only failures count.
- Attempts for unknown emails count too, so the limiter doesn't reveal which emails
  are registered.
- Fails open: if Redis is down, logins are allowed (with a warning) rather than
  locking every user out.
"""

import hashlib
import logging
import time
from functools import cache
from urllib.parse import quote

from limits import RateLimitItemPerSecond
from limits.aio.storage import RedisStorage
from limits.aio.strategies import MovingWindowRateLimiter
from limits.errors import StorageError

from app.core.config import settings
from app.exceptions import RateLimited

logger = logging.getLogger(__name__)


class LoginRateLimiter:
    def __init__(self, storage: RedisStorage) -> None:
        self._limiter = MovingWindowRateLimiter(storage)
        self._item = RateLimitItemPerSecond(
            settings.LOGIN_MAX_FAILED_ATTEMPTS, settings.LOGIN_RATE_LIMIT_WINDOW_SECONDS
        )

    @staticmethod
    def _key(email: str) -> str:
        # Hash so raw email addresses aren't stored in Redis.
        return hashlib.sha256(email.encode()).hexdigest()

    async def register_attempt(self, email: str) -> None:
        """Record an attempt; raise RateLimited (429) once the limit is exceeded."""
        key = self._key(email)
        try:
            if await self._limiter.hit(self._item, "login", key):
                return
            stats = await self._limiter.get_window_stats(self._item, "login", key)
        except StorageError:
            logger.warning("Login rate limiter unavailable; allowing attempt", exc_info=True)
            return
        raise RateLimited(
            retry_after_seconds=int(stats.reset_time - time.time()) + 1,
            message="Too many failed login attempts. Try again later.",
        )

    async def reset(self, email: str) -> None:
        try:
            await self._limiter.clear(self._item, "login", self._key(email))
        except StorageError:
            logger.warning("Could not reset login attempts", exc_info=True)


def _redis_storage_uri() -> str:
    password = settings.REDIS_PASSWORD.get_secret_value() if settings.REDIS_PASSWORD else None
    auth = f":{quote(password, safe='')}@" if password else ""
    return f"async+redis://{auth}{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}"


@cache
def get_login_rate_limiter() -> LoginRateLimiter:
    """FastAPI dependency: one limiter (and Redis connection pool) per process."""
    storage = RedisStorage(
        _redis_storage_uri(),
        # Use redis-py's asyncio client (already a dependency) instead of the
        # default coredis; wrap client errors in limits' StorageError.
        implementation="redispy",
        wrap_exceptions=True,
    )
    return LoginRateLimiter(storage)
