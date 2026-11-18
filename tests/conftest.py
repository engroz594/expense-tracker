"""Test harness: a real PostgreSQL database and a real Redis, clean before every test.

- Settings are pointed at the test database / Redis DB *before* the app is imported,
  because the engine and Redis client are created at import time.
- Migrations run once per session (also proving they apply cleanly).
- Before each test every table is TRUNCATEd and the Redis DB flushed. We truncate
  instead of wrapping each test in a rolled-back transaction because the
  concurrency tests (asyncio.gather) need real, separate connections and commits.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor

# --- must run before anything imports app.* -------------------------------------
os.environ["DB_NAME"] = os.environ.get("TEST_DB_NAME", "expense_tracker_test")
os.environ["REDIS_DB"] = os.environ.get("TEST_REDIS_DB", "15")
# ---------------------------------------------------------------------------------

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import SessionLocal, engine  # noqa: E402
from app.core.redis import redis_client  # noqa: E402
from app.models import Base  # noqa: E402
from main import app  # noqa: E402

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
