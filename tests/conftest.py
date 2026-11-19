"""Test harness: a real PostgreSQL database and a real Redis, clean before every test.

- Settings are pointed at the test database / Redis DB *before* the app is imported,
  because the engine and Redis client are created at import time.
- Migrations run once per session (also proving they apply cleanly).
- Before each test every table is TRUNCATEd and the Redis DB flushed. We truncate
  instead of wrapping each test in a rolled-back transaction because the
  concurrency tests (asyncio.gather) need real, separate connections and commits.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor

# --- must run before anything imports app.* -------------------------------------
os.environ["DB_NAME"] = os.environ.get("TEST_DB_NAME", "expense_tracker_test")
os.environ["REDIS_DB"] = os.environ.get("TEST_REDIS_DB", "15")
# A fixed, test-only signing key (not a real secret), so tests don't depend on .env.
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-not-used-anywhere-real-0123456789")
# ---------------------------------------------------------------------------------

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import SessionLocal, engine  # noqa: E402
from app.core.redis import redis_client  # noqa: E402
from app.models import Base, User  # noqa: E402
from app.scripts.fill_exchange_rates import fill_exchange_rates  # noqa: E402
from main import app  # noqa: E402
from tests.helpers import (  # noqa: E402
    DEFAULT_EMAIL,
    DEFAULT_PASSWORD,
    OTHER_EMAIL,
    TODAY,
    bearer,
)

# Guard: TRUNCATE below wipes every table. Refuse to run against anything that
# isn't clearly a test database.
assert settings.DB_NAME.endswith("_test"), (
    f"Refusing to run tests against DB_NAME={settings.DB_NAME!r} (must end with '_test')"
)


def run_in_fresh_thread(fn, *args) -> None:
    """Run fn in a worker thread and wait for it.

    Used for code that calls asyncio.run() (Alembic's env.py, the DB bootstrap below),
    which raises if the calling thread already has a running event loop — and
    pytest-asyncio's loop may be running. A fresh thread has no loop.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(fn, *args).result()


async def _create_test_database_if_missing() -> None:
    # CREATE DATABASE can't run inside a transaction, hence AUTOCOMMIT, and it must be
    # issued from another database — the "postgres" maintenance DB.
    admin_url = settings.database_url.set(database="postgres")
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": settings.DB_NAME},
            )
            if not exists:
                # Identifier can't be a bind parameter; the name is guarded above.
                await conn.execute(text(f'CREATE DATABASE "{settings.DB_NAME}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    try:
        run_in_fresh_thread(asyncio.run, _create_test_database_if_missing())
    except Exception as exc:
        pytest.exit(
            f"Could not create test database {settings.DB_NAME!r} ({exc}). Create it "
            f"manually, or use a DB_USER with the CREATEDB privilege.",
            returncode=1,
        )
    run_in_fresh_thread(command.upgrade, Config("alembic.ini"), "head")
    yield


@pytest.fixture(scope="session", autouse=True)
async def close_connections() -> AsyncIterator[None]:
    yield
    await redis_client.aclose()
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_state(migrated_database: None) -> AsyncIterator[None]:
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await redis_client.flushdb()
    yield


# --- S3 (moto) -------------------------------------------------------------------------
# Brief: never call real S3 in tests. moto runs a fake S3 as a local HTTP server
# (server mode, not in-process mock_aws, because aiobotocore talks real HTTP through
# aiohttp, which in-process mocking can't intercept reliably).

TEST_BUCKET = "expense-tracker-test"


@pytest.fixture(scope="session")
def moto_s3_endpoint() -> Iterator[str]:
    from moto.server import ThreadedMotoServer

    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # no request log per call
    server = ThreadedMotoServer(port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    endpoint = f"http://{host}:{port}"
    with pytest.MonkeyPatch.context() as patch:
        # S3StorageClient reads these at call time.
        patch.setattr(settings, "S3_ENDPOINT_URL", endpoint)
        patch.setattr(settings, "S3_BUCKET", TEST_BUCKET)
        patch.setattr(settings, "S3_ACCESS_KEY_ID", SecretStr("test"))
        patch.setattr(settings, "S3_SECRET_ACCESS_KEY", SecretStr("test"))
        yield endpoint
    server.stop()


@pytest.fixture
async def s3_bucket(moto_s3_endpoint: str) -> str:
    """An empty test bucket: moto's state is reset, then the bucket is recreated."""
    from app.clients.storage.s3_storage_client import S3StorageClient

    async with AsyncClient() as http:
        await http.post(f"{moto_s3_endpoint}/moto-api/reset")
    async with S3StorageClient()._client() as s3:
        await s3.create_bucket(Bucket=TEST_BUCKET)
    return TEST_BUCKET


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """A session for arranging data and asserting on it directly in the database."""
    async with SessionLocal() as session:
        yield session


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # raise_app_exceptions=False: let unhandled errors reach our 500 handler and come
    # back as a response, exactly as a real client would see them.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# --- auth helpers (used by every feature slice) -----------------------------------


@pytest.fixture
def register_user(client: AsyncClient):
    async def _register(
        email: str = DEFAULT_EMAIL, password: str = DEFAULT_PASSWORD, base_currency: str = "PKR"
    ) -> dict:
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "base_currency": base_currency},
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _register


@pytest.fixture
def login(client: AsyncClient):
    async def _login(email: str = DEFAULT_EMAIL, password: str = DEFAULT_PASSWORD) -> dict:
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 200, response.text
        return response.json()

    return _login


@pytest.fixture
async def auth_headers(register_user, login) -> dict[str, str]:
    """Headers for a freshly registered and logged-in default user."""
    await register_user()
    return bearer(await login())


@pytest.fixture
async def auth_user_id(auth_headers, db: AsyncSession) -> uuid.UUID:
    """The id of the user behind auth_headers."""
    return await db.scalar(select(User.id).where(User.email == DEFAULT_EMAIL))


@pytest.fixture
async def other_auth_headers(register_user, login) -> dict[str, str]:
    """A second user, for "can't see/change someone else's data" tests."""
    await register_user(email=OTHER_EMAIL)
    return bearer(await login(email=OTHER_EMAIL))


# --- data helpers ---------------------------------------------------------------------


@pytest.fixture
async def rates(db: AsyncSession) -> None:
    """Exchange rates for the 90 days up to today (UTC), as the fill script makes them."""
    await fill_exchange_rates(db, until=TODAY)


@pytest.fixture
def create_category(client: AsyncClient):
    async def _create(headers: dict, name: str = "Food", monthly_budget: str | None = None) -> dict:
        response = await client.post(
            "/api/v1/categories",
            json={"name": name, "monthly_budget": monthly_budget},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _create


@pytest.fixture
def create_expense(client: AsyncClient):
    async def _create(headers: dict, category_id: str, **overrides) -> dict:
        payload = {
            "category_id": category_id,
            "amount": "12.50",
            "currency": "PKR",
            "description": "Lunch",
            "spent_on": TODAY.isoformat(),
        } | overrides
        response = await client.post("/api/v1/expenses", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    return _create
