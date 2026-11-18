from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Currency
from app.models import ExchangeRate
from app.scripts.fill_exchange_rates import DAYS, fill_exchange_rates, rate_for

UNTIL = date(2026, 10, 31)


async def count_rates(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).select_from(ExchangeRate))


async def test_first_run_fills_every_currency_for_every_day_of_the_window(db: AsyncSession):
    inserted = await fill_exchange_rates(db, until=UNTIL)

    assert inserted == DAYS * len(Currency)
    first, last, days = (
        await db.execute(
            select(
                func.min(ExchangeRate.valid_on),
                func.max(ExchangeRate.valid_on),
                func.count(func.distinct(ExchangeRate.valid_on)),
            )
        )
    ).one()
    assert (first, last, days) == (UNTIL - timedelta(days=DAYS - 1), UNTIL, DAYS)


async def test_running_again_inserts_nothing(db: AsyncSession):
    await fill_exchange_rates(db, until=UNTIL)

    assert await fill_exchange_rates(db, until=UNTIL) == 0
    assert await count_rates(db) == DAYS * len(Currency)


async def test_only_missing_days_are_filled(db: AsyncSession):
    await fill_exchange_rates(db, until=UNTIL)
    await db.execute(delete(ExchangeRate).where(ExchangeRate.valid_on > UNTIL - timedelta(days=3)))
    await db.commit()

    assert await fill_exchange_rates(db, until=UNTIL) == 3 * len(Currency)


async def test_a_later_run_adds_only_the_new_latest_days(db: AsyncSession):
    await fill_exchange_rates(db, until=UNTIL)

    inserted = await fill_exchange_rates(db, until=UNTIL + timedelta(days=2))

    assert inserted == 2 * len(Currency)


async def test_existing_rates_are_never_overwritten(db: AsyncSession):
    db.add(ExchangeRate(currency="EUR", valid_on=UNTIL, rate_to_usd=Decimal("2.5")))
    await db.commit()

    await fill_exchange_rates(db, until=UNTIL)

    rate = await db.scalar(
        select(ExchangeRate.rate_to_usd).where(
            ExchangeRate.currency == "EUR", ExchangeRate.valid_on == UNTIL
        )
    )
    assert rate == Decimal("2.5")


async def test_generated_rates_are_deterministic_decimals():
    monday, tuesday = date(2026, 11, 16), date(2026, 11, 17)

    assert rate_for(Currency.USD, monday) == Decimal("1")
    # AED is pegged: same every day. 1 USD = 3.6725 AED.
    assert rate_for(Currency.AED, monday) == rate_for(Currency.AED, tuesday)
    assert rate_for(Currency.AED, monday) == Decimal("0.2722940776")
    # Floating currencies move between days, but the same day always gives the same rate.
    assert rate_for(Currency.EUR, monday) != rate_for(Currency.EUR, tuesday)
    assert rate_for(Currency.PKR, monday) == rate_for(Currency.PKR, monday)
    # Quantised to NUMERIC(20, 10).
    assert all(rate_for(c, monday).as_tuple().exponent == -10 for c in Currency)
