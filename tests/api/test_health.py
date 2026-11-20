import asyncio

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.redis import get_redis
from main import app


class _RedisDown:
    async def ping(self) -> bool:
        raise ConnectionError("redis is down")


class _RedisHung:
    async def ping(self) -> bool:
        await asyncio.sleep(5)
        return True


@pytest.fixture
def override_redis():
    def _override(fake: object) -> None:
        app.dependency_overrides[get_redis] = lambda: fake

    yield _override
    app.dependency_overrides.pop(get_redis, None)


async def test_root_returns_application_info_without_checking_dependencies(
    client: AsyncClient, override_redis
):
    override_redis(_RedisDown())  # like /health, the root must not depend on Redis/Postgres

    response = await client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


async def test_health_is_ok_without_checking_dependencies(client: AsyncClient, override_redis):
    override_redis(_RedisDown())  # liveness must not depend on Redis/Postgres

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_is_ok_when_postgres_and_redis_respond(client: AsyncClient):
    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


async def test_ready_returns_503_envelope_when_redis_is_down(client: AsyncClient, override_redis):
    override_redis(_RedisDown())

    response = await client.get("/ready")

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "service_unavailable"
    assert {"check": "database", "ok": True} in error["details"]
    assert {"check": "redis", "ok": False} in error["details"]


async def test_ready_fails_fast_when_a_dependency_hangs(
    client: AsyncClient, override_redis, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "READINESS_TIMEOUT_SECONDS", 0.05)
    override_redis(_RedisHung())

    response = await asyncio.wait_for(client.get("/ready"), timeout=2)

    assert response.status_code == 503
    assert {"check": "redis", "ok": False} in response.json()["error"]["details"]
