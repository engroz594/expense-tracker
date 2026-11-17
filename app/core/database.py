from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# One engine (and connection pool) per process. asyncpg is the async driver, so
# no database call ever blocks the event loop.
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    echo=settings.DB_ECHO,
)

# expire_on_commit=False: after commit we still serialise the ORM objects into the
# response; with expiry on, touching an attribute would trigger a new (implicit) query.
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, always closed afterwards.

    Services own the transaction (they call commit). Anything not committed is
    rolled back when the session closes.
    """
    async with SessionLocal() as session:
        yield session
