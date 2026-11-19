"""Idempotency-Key handling for POST /expenses, kept entirely in Redis.

One Redis key per (user, Idempotency-Key), holding a small JSON record:

    {"state": "processing", "hash": H}                       while the request runs
    {"state": "done", "hash": H, "status": 201, "body": ...}  once it succeeded

The race (brief: two requests with the same key at the same moment must create
only one expense) is handled by a Redis lock: SET ... NX. NX ("only if not
exists") is atomic in Redis, so exactly one request can claim a key, however many
arrive at once and whichever API server they hit. A request that finds the key
already claimed never runs the operation:
  - different body hash        -> 422 (key reused for a different request)
  - same hash, still running   -> 409 (request in progress; retry shortly)
  - same hash, done            -> replay the stored first response, no new expense
Codes as in the IETF draft "The Idempotency-Key HTTP Header Field".

Expiry: the claim is set with EX 86400 and the completion keeps that TTL, so a key
lives 24 hours from first use and Redis deletes it itself — no cleanup job.

Failure behaviour, chosen so that a duplicate expense is never possible:
  - The operation fails (e.g. 404 category): the claim is released, so the client
    can retry with the same key.
  - The process dies after the expense committed but before "done" was written:
    the key stays "processing" and retries get 409 until it expires. A stuck retry,
    never a duplicate. (Releasing it instead could create a second expense.)
  - Redis is unreachable: 503 for requests that carry an Idempotency-Key (fail
    closed) — without Redis we can't promise "only once". Requests without the
    header are unaffected.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings
from app.exceptions import Conflict, ServiceUnavailable, UnprocessableEntity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str]
    replayed: bool


Operation = Callable[[], Awaitable[tuple[int, dict[str, Any], dict[str, str]]]]


class IdempotencyService:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    @staticmethod
    def _redis_key(user_id: uuid.UUID, idempotency_key: str) -> str:
        # Scoped per user: two users can't collide on, or probe, each other's keys.
        # The client's key is hashed so the Redis key has a fixed, safe length.
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        return f"idempotency:{user_id}:{digest}"

    async def run(
        self,
        user_id: uuid.UUID,
        idempotency_key: str,
        request_hash: str,
        operation: Operation,
    ) -> StoredResponse:
        """Run `operation` at most once per key; replay its response afterwards."""
        key = self._redis_key(user_id, idempotency_key)
        stored = await self._claim_or_get(key, request_hash)
        if stored is not None:
            return stored

        try:
            status, body, headers = await operation()
        except Exception:
            await self._release(key)
            raise

        record = {
            "state": "done",
            "hash": request_hash,
            "status": status,
            "body": body,
            "headers": headers,
        }
        try:
            # XX: only overwrite our own claim. KEEPTTL: the 24h counts from first use.
            await self.redis.set(key, json.dumps(record), xx=True, keepttl=True)
        except RedisError:
            # The expense is committed; the client gets its 201. Retries will see
            # "processing" (409) until the key expires — never a duplicate.
            logger.error("Could not store idempotent response for %s", key, exc_info=True)
        return StoredResponse(status, body, headers, replayed=False)

    async def _claim_or_get(self, key: str, request_hash: str) -> StoredResponse | None:
        claim = json.dumps({"state": "processing", "hash": request_hash})
        try:
            # A key can vanish between a failed SET NX and the GET (expiry or a
            # failed request releasing it), so try twice before giving up.
            for _ in range(2):
                if await self.redis.set(
                    key, claim, nx=True, ex=settings.IDEMPOTENCY_KEY_TTL_SECONDS
                ):
                    return None  # claimed: this request runs the operation
                raw = await self.redis.get(key)
                if raw is not None:
                    return self._existing(json.loads(raw), request_hash)
        except RedisError:
            logger.warning("Idempotency store unavailable", exc_info=True)
            raise ServiceUnavailable(
                "Idempotency is temporarily unavailable; retry shortly",
                code="idempotency_unavailable",
            ) from None
        raise Conflict("Idempotency-Key is busy; retry shortly", code="request_in_progress")

    @staticmethod
    def _existing(record: dict[str, Any], request_hash: str) -> StoredResponse:
        if record["hash"] != request_hash:
            raise UnprocessableEntity(
                "This Idempotency-Key was already used with a different request body",
                code="idempotency_key_reused",
            )
        if record["state"] != "done":
            raise Conflict(
                "A request with this Idempotency-Key is still in progress",
                code="request_in_progress",
            )
        return StoredResponse(
            record["status"], record["body"], record.get("headers", {}), replayed=True
        )

    async def _release(self, key: str) -> None:
        try:
            await self.redis.delete(key)
        except RedisError:
            logger.warning("Could not release idempotency key %s", key, exc_info=True)


def fingerprint(payload: dict[str, Any]) -> str:
    """SHA-256 of a canonical JSON form, so key order and whitespace don't matter."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
