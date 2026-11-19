"""Monthly report: per-category totals in the user's base currency, with budget use
and the change from the previous month — computed by ONE SQL statement.

Shape of the query:
  converted     every expense of the previous and the requested month, converted to
                the base currency with the rate valid on its date (shared CTE)
  months        the two month starts (previous, requested)
  monthly       categories CROSS JOIN months, LEFT JOIN converted, GROUP BY —
                so a category with no spend still gets a 0 row for each month
  with_previous LAG(total) OVER (PARTITION BY category ORDER BY month) puts last
                month's total next to this month's
  final SELECT  keeps the requested month, adds percent used and the change,
                rounds to 2 places once, at the end
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Uuid, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.services.currency_service import CONVERTED_EXPENSES_CTE, shift_month

_MONTHLY_REPORT_SQL = text(
    f"""
WITH {CONVERTED_EXPENSES_CTE},
months AS (
    SELECT CAST(:previous_month AS date) AS month_start
    UNION ALL
    SELECT CAST(:report_month AS date)
),
monthly AS (
    SELECT c.id AS category_id,
           c.name,
           c.monthly_budget,
           m.month_start,
           COALESCE(SUM(cv.amount_base), 0) AS total
    FROM categories c
    CROSS JOIN months m
    LEFT JOIN converted cv
           ON cv.category_id = c.id
          AND cv.spent_on >= m.month_start
          AND cv.spent_on < m.month_start + INTERVAL '1 month'
    WHERE c.user_id = :user_id
    GROUP BY c.id, c.name, c.monthly_budget, m.month_start
),
with_previous AS (
    SELECT monthly.*,
           LAG(total) OVER (PARTITION BY category_id ORDER BY month_start) AS previous_total
    FROM monthly
)
SELECT category_id,
       name,
       monthly_budget AS budget,
       ROUND(total, 2) AS total,
       CASE WHEN monthly_budget IS NOT NULL
            THEN ROUND(total * 100 / monthly_budget, 2) END AS percent_used,
       ROUND(previous_total, 2) AS previous_total,
       ROUND(total - previous_total, 2) AS change,
       CASE WHEN previous_total > 0
            THEN ROUND((total - previous_total) * 100 / previous_total, 2) END
           AS change_percent
FROM with_previous
WHERE month_start = :report_month
ORDER BY total DESC, name
"""
).bindparams(
    bindparam("user_id", type_=Uuid),
    bindparam("period_start", type_=Date),
    bindparam("period_end", type_=Date),
    bindparam("previous_month", type_=Date),
    bindparam("report_month", type_=Date),
)


@dataclass(frozen=True)
class CategoryReportRow:
    category_id: uuid.UUID
    name: str
    budget: Decimal | None
    total: Decimal
    percent_used: Decimal | None  # null when the category has no budget
    previous_total: Decimal
    change: Decimal
    change_percent: Decimal | None  # null when last month was 0 (no meaningful %)


class ReportService:
    def __init__(self, db: AsyncSession, user: User) -> None:
        self.db = db
        self.user = user

    async def monthly(self, year: int, month: int) -> list[CategoryReportRow]:
        report_month = date(year, month, 1)
        previous_month = shift_month(report_month, -1)
        result = await self.db.execute(
            _MONTHLY_REPORT_SQL,
            {
                "user_id": self.user.id,
                "base_currency": self.user.base_currency,
                # converted only needs the two months the report looks at.
                "period_start": previous_month,
                "period_end": shift_month(report_month, 1),
                "previous_month": previous_month,
                "report_month": report_month,
            },
        )
        return [CategoryReportRow(**row) for row in result.mappings()]
