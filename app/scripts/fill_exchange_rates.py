"""Fill exchange rates for the last 90 days (up to today, UTC).

    uv run python -m app.scripts.fill_exchange_rates

Safe to run at any time, as often as needed: only (currency, day) pairs that are
missing get inserted (ON CONFLICT DO NOTHING), existing rates are never changed.
Run it after the first `alembic upgrade head`, and again whenever the latest days
should be filled in — skipped days inside the window are filled too.

Why a script and not a migration: a migration runs exactly once per database, but
rates are data that keeps growing every day.

The rates are illustrative but realistic and fully deterministic (a given day always
gets the same values), stored as rate_to_usd = USD value of one unit:
  - USD is the pivot, always 1.
  - AED is pegged: 1 USD = 3.6725 AED.
  - EUR and PKR move around a base by a fixed weekly pattern of basis-point offsets,
    so different days genuinely have different rates.
All arithmetic is Decimal — no floats anywhere near money.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal, engine
from app.enums import Currency
from app.models import ExchangeRate

DAYS = 90

# USD value of one unit of each currency.
BASE_RATE_TO_USD: dict[Currency, Decimal] = {
    Currency.USD: Decimal("1"),
    Currency.AED: Decimal("1") / Decimal("3.6725"),
    Currency.EUR: Decimal("1.08"),
    Currency.PKR: Decimal("1") / Decimal("280"),
}
# Currencies that float; USD and AED (pegged) stay constant.
FLOATING = {Currency.EUR, Currency.PKR}
# Basis-point offsets cycled by day, e.g. +35 bp = rate * 1.0035.
WEEKLY_OFFSETS_BP = [0, 35, 60, 20, -15, -45, -25]

QUANT = Decimal("0.0000000001")  # matches NUMERIC(20, 10)


def rate_for(currency: Currency, day: date) -> Decimal:
    base = BASE_RATE_TO_USD[currency]
    if currency not in FLOATING:
        return base.quantize(QUANT)
    offset_bp = Decimal(WEEKLY_OFFSETS_BP[day.toordinal() % len(WEEKLY_OFFSETS_BP)])
    return (base * (1 + offset_bp / 10_000)).quantize(QUANT)


async def fill_exchange_rates(
    session: AsyncSession, until: date | None = None, days: int = DAYS
) -> int:
    """Insert the missing rates for the `days` days ending at `until` (default: today
    UTC). Returns how many rows were inserted."""
    until = until or datetime.now(UTC).date()
    rows = [
        {"currency": currency.value, "valid_on": day, "rate_to_usd": rate_for(currency, day)}
        for day in (until - timedelta(days=offset) for offset in range(days))
        for currency in Currency
    ]
    # RETURNING only yields the rows actually inserted (conflicts are skipped),
    # which gives an exact count of what was missing.
    result = await session.execute(
        insert(ExchangeRate)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["currency", "valid_on"])
        .returning(ExchangeRate.currency)
    )
    inserted = len(result.all())
    await session.commit()
    return inserted


async def main() -> None:
    try:
        async with SessionLocal() as session:
            inserted = await fill_exchange_rates(session)
    finally:
        await engine.dispose()
    total = DAYS * len(Currency)
    print(f"Exchange rates: {inserted} inserted, {total - inserted} already present.")


if __name__ == "__main__":
    asyncio.run(main())
