import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  — registers every model on Base.metadata for autogenerate
from app.core.config import settings
from app.models.base import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: when migrations run in-process (the test suite),
    # the default would silence every logger the app had already created.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`: render SQL to stdout instead of executing it."""
    context.configure(
        url=settings.database_url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Same asyncpg driver as the app — no sync Postgres driver is installed at all.
    # NullPool: a migration run is one short-lived connection; no pool to keep around.
    engine = create_async_engine(settings.database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    # asyncio.run() needs a thread with no running event loop. The CLI is fine;
    # the test suite therefore runs `alembic upgrade` from a sync fixture/thread.
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
