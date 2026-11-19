"""Exchange-rate rules shared by every feature that touches money in more than one
currency (expense writes now; the monthly report, category spend and budget alerts
later reuse this module's conversion).

Rule: an amount in currency C on day d converts with the latest rate for C whose
valid_on <= d (a day with no rate falls back to the previous one).
"""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import UnprocessableEntity
from app.models import ExchangeRate


async def ensure_rates_available(db: AsyncSession, currencies: set[str], day: date) -> None:
    """Raise 422 unless every currency has a rate on or before `day`.

    Checked on every expense write, for the expense's currency and the user's base
    currency, so every stored expense is guaranteed convertible: the report's joins
    can then never silently drop an expense for lack of a rate.
    Served by the exchange_rates (currency, valid_on) primary key.
    """
    covered = await db.scalar(
        select(func.count(func.distinct(ExchangeRate.currency))).where(
            ExchangeRate.currency.in_(currencies), ExchangeRate.valid_on <= day
        )
    )
    if covered != len(currencies):
        raise UnprocessableEntity(
            f"No exchange rate available on or before {day.isoformat()}",
            code="no_exchange_rate",
        )
