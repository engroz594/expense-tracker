import asyncio
import json
import uuid

import pytest
from httpx import AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import get_redis, redis_client
from app.models import Expense
from app.services.idempotency_service import IdempotencyService
from main import app
from tests.helpers import TODAY

EXPENSES = "/api/v1/expenses"

pytestmark = pytest.mark.usefixtures("rates")


@pytest.fixture
async def food(auth_headers, create_category) -> dict:
    return await create_category(auth_headers, name="Food")


@pytest.fixture
def body(food) -> dict:
    return {
        "category_id": food["id"],
        "amount": "12.50",
        "currency": "PKR",
        "description": "Lunch",
        "spent_on": TODAY.isoformat(),
    }


async def post(client: AsyncClient, headers: dict, body: dict, key: str | None = "key-1"):
    extra = {"Idempotency-Key": key} if key is not None else {}
    return await client.post(EXPENSES, json=body, headers=headers | extra)


async def expense_count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).select_from(Expense))


async def test_same_key_and_body_replays_the_first_response(
    client: AsyncClient, auth_headers, body, db: AsyncSession
):
    first = await post(client, auth_headers, body)
    second = await post(client, auth_headers, body)

    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json() == first.json()
    assert second.headers["etag"] == first.headers["etag"]
    assert second.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert await expense_count(db) == 1


async def test_equivalent_body_counts_as_the_same_request(
    client: AsyncClient, auth_headers, body, db: AsyncSession
):
    await post(client, auth_headers, body)
    same_meaning = {k: body[k] for k in reversed(list(body))} | {"amount": 12.5}

    response = await post(client, auth_headers, same_meaning)

    assert response.status_code == 201
    assert response.headers["idempotent-replayed"] == "true"
    assert await expense_count(db) == 1


async def test_same_key_with_different_body_is_422(
    client: AsyncClient, auth_headers, body, db: AsyncSession
):
    await post(client, auth_headers, body)

    response = await post(client, auth_headers, body | {"amount": "99.00"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "idempotency_key_reused"
    assert await expense_count(db) == 1


async def test_parallel_posts_with_same_key_create_only_one_expense(
    client: AsyncClient, auth_headers, body, db: AsyncSession
):
    """Brief: two parallel POSTs with the same Idempotency-Key create only one expense."""
    responses = await asyncio.gather(*(post(client, auth_headers, body) for _ in range(2)))

    assert await expense_count(db) == 1
    codes = sorted(r.status_code for r in responses)
    # The winner gets 201. The other either arrived while the winner was running
    # (409 "in progress") or just after it finished (201 replay of the same body).
    assert codes in ([201, 201], [201, 409])
    created = [r.json() for r in responses if r.status_code == 201]
    assert all(c == created[0] for c in created)


async def test_many_parallel_posts_still_create_only_one_expense(
    client: AsyncClient, auth_headers, body, db: AsyncSession
):
    responses = await asyncio.gather(*(post(client, auth_headers, body) for _ in range(10)))

    assert await expense_count(db) == 1
    assert {r.status_code for r in responses} <= {201, 409}


async def test_keys_are_scoped_per_user(
    client: AsyncClient, auth_headers, other_auth_headers, create_category, body, db
):
    bobs = await create_category(other_auth_headers, name="Food")

    alice = await post(client, auth_headers, body, key="shared")
    bob = await post(client, other_auth_headers, body | {"category_id": bobs["id"]}, key="shared")

    assert (alice.status_code, bob.status_code) == (201, 201)
    assert "idempotent-replayed" not in bob.headers
    assert await expense_count(db) == 2


async def test_without_the_header_every_post_creates_an_expense(
    client: AsyncClient, auth_headers, body, db: AsyncSession
):
    await post(client, auth_headers, body, key=None)
    await post(client, auth_headers, body, key=None)

    assert await expense_count(db) == 2


async def test_failed_request_releases_the_key_for_a_retry(
    client: AsyncClient, auth_headers, body, create_category, db: AsyncSession
):
    unknown = body | {"category_id": str(uuid.uuid4())}

    first = await post(client, auth_headers, unknown)
    retry = await post(client, auth_headers, unknown)

    # Both actually ran (404 each time) rather than the retry being stuck at 409.
    assert (first.status_code, retry.status_code) == (404, 404)


async def test_key_lives_24_hours_then_can_be_reused(
    client: AsyncClient, auth_headers, body, db: AsyncSession, auth_user_id
):
    await post(client, auth_headers, body)
    redis_key = IdempotencyService._redis_key(auth_user_id, "key-1")

    ttl = await redis_client.ttl(redis_key)
    assert 24 * 3600 - 60 < ttl <= 24 * 3600

    await redis_client.delete(redis_key)  # what Redis does when the TTL runs out
    after_expiry = await post(client, auth_headers, body)

    assert after_expiry.status_code == 201
    assert "idempotent-replayed" not in after_expiry.headers
    assert await expense_count(db) == 2


async def test_request_still_in_progress_is_409(
    client: AsyncClient, auth_headers, body, db: AsyncSession, auth_user_id
):
    from app.schemas.expense_schemas import ExpenseCreate
    from app.services.idempotency_service import fingerprint

    redis_key = IdempotencyService._redis_key(auth_user_id, "key-1")
    in_flight = {"state": "processing", "hash": fingerprint(ExpenseCreate(**body).canonical())}
    await redis_client.set(redis_key, json.dumps(in_flight))

    response = await post(client, auth_headers, body)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "request_in_progress"
    assert await expense_count(db) == 0


class _RedisDown:
    async def set(self, *args, **kwargs):
        raise RedisConnectionError("redis is down")

    async def get(self, *args, **kwargs):
        raise RedisConnectionError("redis is down")


async def test_redis_down_fails_closed_only_for_idempotent_requests(
    client: AsyncClient, auth_headers, body, db: AsyncSession
):
    app.dependency_overrides[get_redis] = lambda: _RedisDown()
    try:
        with_key = await post(client, auth_headers, body)
        without_key = await post(client, auth_headers, body, key=None)
    finally:
        app.dependency_overrides.pop(get_redis, None)

    assert with_key.status_code == 503
    assert with_key.json()["error"]["code"] == "idempotency_unavailable"
    assert without_key.status_code == 201
    assert await expense_count(db) == 1


@pytest.mark.parametrize("key", ["", "x" * 256])
async def test_invalid_idempotency_key_header_is_422(client: AsyncClient, auth_headers, body, key):
    response = await post(client, auth_headers, body, key=key)

    assert response.status_code == 422
