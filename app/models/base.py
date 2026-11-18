from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint/index names. Without this Postgres invents names, and a
# later migration that drops or alters a constraint can't refer to it reliably.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def currency_check(column: str) -> str:
    """SQL for a CHECK that limits a currency column to the supported codes.

    Defence in depth: the API validates currencies too, but bulk paths (CSV import
    via Core inserts) also hit the database, and the DB should never hold a currency
    it has no exchange rates for.
    """
    from app.enums import Currency

    codes = ", ".join(f"'{c.value}'" for c in Currency)
    return f"{column} IN ({codes})"


class Base(DeclarativeBase):
    """Declarative base for all models.

    Models deliberately define no relationship() attributes: in async SQLAlchemy a
    lazy load raises instead of querying, so every query states its joins explicitly.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
