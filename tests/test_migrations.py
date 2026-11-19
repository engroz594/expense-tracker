import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine


async def alembic(action: str, *args: str) -> None:
    # In a thread: env.py calls asyncio.run(), which can't run inside this test's loop.
    await asyncio.to_thread(getattr(command, action), Config("alembic.ini"), *args)


async def test_models_and_migrations_are_in_sync():
    # `alembic check` raises if autogenerate would produce any operation, i.e. a model
    # was changed without a matching migration.
    await alembic("check")


async def test_every_migration_downgrades_and_upgrades_cleanly(db: AsyncSession):
    await alembic("downgrade", "base")
    await alembic("upgrade", "head")

    # Tables were dropped and recreated; drop pooled connections so asyncpg doesn't
    # reuse prepared statements that point at the old tables.
    await engine.dispose()
    tables = await db.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    )
    assert [row[0] for row in tables] == [
        "alembic_version",
        "categories",
        "exchange_rates",
        "expenses",
        "refresh_tokens",
        "users",
    ]
