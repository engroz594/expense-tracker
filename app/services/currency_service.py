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

# A CTE for use inside larger single-statement queries: every expense of :user_id
# with spent_on in [:period_start, :period_end), converted to :base_currency.
#
#   amount_base = amount * rate_to_usd(expense currency) / rate_to_usd(base currency)
#
# Each rate is the latest with valid_on <= spent_on (CROSS JOIN LATERAL ... ORDER BY
# valid_on DESC LIMIT 1): a day without a rate falls back to the previous one. Each
# lookup is one backward probe of the exchange_rates (currency, valid_on) primary key.
# Rounding happens only at the very end of the outer query — never per expense.
#
# CROSS (inner) lateral joins are safe because expense writes require a rate on or
# before spent_on for both currencies (ensure_rates_available), so no expense can be
# dropped here for lack of a rate.
CONVERTED_EXPENSES_CTE = """
converted AS (
    SELECT e.category_id,
           e.spent_on,
           e.amount * src.rate_to_usd / base.rate_to_usd AS amount_base
    FROM expenses e
    CROSS JOIN LATERAL (
        SELECT r.rate_to_usd
        FROM exchange_rates r
        WHERE r.currency = e.currency AND r.valid_on <= e.spent_on
        ORDER BY r.valid_on DESC
        LIMIT 1
    ) src
    CROSS JOIN LATERAL (
        SELECT r.rate_to_usd
        FROM exchange_rates r
        WHERE r.currency = :base_currency AND r.valid_on <= e.spent_on
        ORDER BY r.valid_on DESC
        LIMIT 1
    ) base
    WHERE e.user_id = :user_id
      AND e.spent_on >= :period_start
      AND e.spent_on < :period_end
)
"""


def month_start(day: date) -> date:
    return day.replace(day=1)


def shift_month(first_of_month: date, months: int) -> date:
    index = first_of_month.year * 12 + (first_of_month.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


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
