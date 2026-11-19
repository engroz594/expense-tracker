import asyncio

from httpx import AsyncClient
from limits.aio.storage import RedisStorage

from app.services.rate_limit_service import LoginRateLimiter, get_login_rate_limiter
from main import app
from tests.helpers import DEFAULT_PASSWORD

LOGIN = "/api/v1/auth/login"


async def attempt(client: AsyncClient, password: str = "wrong-password", email="alice@example.com"):
    return await client.post(LOGIN, json={"email": email, "password": password})


async def test_sixth_failed_login_returns_429_with_retry_after(client: AsyncClient, register_user):
    await register_user()

    for _ in range(5):
        assert (await attempt(client)).status_code == 401
    sixth = await attempt(client)

    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "rate_limited"
    assert 0 < int(sixth.headers["retry-after"]) <= 15 * 60
    # Even the right password is refused until the window passes.
    assert (await attempt(client, password=DEFAULT_PASSWORD)).status_code == 429


async def test_successful_login_resets_the_count(client: AsyncClient, register_user):
    await register_user()
    for _ in range(4):
        await attempt(client)

    assert (await attempt(client, password=DEFAULT_PASSWORD)).status_code == 200

    for _ in range(5):
        assert (await attempt(client)).status_code == 401
    assert (await attempt(client)).status_code == 429


async def test_limit_is_per_email(client: AsyncClient, register_user):
    await register_user(email="alice@example.com")
    await register_user(email="bob@example.com")
    for _ in range(6):
        await attempt(client, email="alice@example.com")

    assert (await attempt(client, email="bob@example.com")).status_code == 401


async def test_unknown_emails_are_limited_too(client: AsyncClient):
    for _ in range(5):
        assert (await attempt(client, email="ghost@example.com")).status_code == 401

    assert (await attempt(client, email="ghost@example.com")).status_code == 429


async def test_parallel_guesses_cannot_exceed_the_limit(client: AsyncClient, register_user):
    await register_user()

    responses = await asyncio.gather(*(attempt(client) for _ in range(12)))

    codes = sorted(r.status_code for r in responses)
    assert codes.count(401) == 5
    assert codes.count(429) == 7


async def test_limiter_fails_open_when_redis_is_down(client: AsyncClient, register_user):
    await register_user()
    unreachable = RedisStorage(
        "async+redis://localhost:1/0",
        implementation="redispy",
        wrap_exceptions=True,
        socket_connect_timeout=0.2,
    )
    app.dependency_overrides[get_login_rate_limiter] = lambda: LoginRateLimiter(unreachable)
    try:
        responses = [await attempt(client) for _ in range(7)]
        ok = await attempt(client, password=DEFAULT_PASSWORD)
    finally:
        app.dependency_overrides.pop(get_login_rate_limiter, None)

    assert all(r.status_code == 401 for r in responses)  # never 429 without Redis
    assert ok.status_code == 200
