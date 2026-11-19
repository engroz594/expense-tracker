import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Date, Uuid, bindparam, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import Conflict, NotFound
from app.models import Category, User
from app.services.currency_service import CONVERTED_EXPENSES_CTE, month_start, shift_month

# My categories with this month's spend (in the base currency) vs budget, in one
# statement — the same conversion as the monthly report.
_CATEGORIES_WITH_SPEND_SQL = text(
    f"""
WITH {CONVERTED_EXPENSES_CTE}
SELECT c.id,
       c.name,
       c.monthly_budget,
       c.created_at,
       ROUND(COALESCE(SUM(cv.amount_base), 0), 2) AS spent_this_month,
       CASE WHEN c.monthly_budget IS NOT NULL
            THEN ROUND(COALESCE(SUM(cv.amount_base), 0) * 100 / c.monthly_budget, 2) END
           AS percent_used
FROM categories c
LEFT JOIN converted cv ON cv.category_id = c.id
WHERE c.user_id = :user_id
GROUP BY c.id
ORDER BY c.name
"""
).bindparams(
    bindparam("user_id", type_=Uuid),
    bindparam("period_start", type_=Date),
    bindparam("period_end", type_=Date),
)


@dataclass(frozen=True)
class CategoryWithSpend:
    id: uuid.UUID
    name: str
    monthly_budget: Decimal | None
    created_at: datetime
    spent_this_month: Decimal
    percent_used: Decimal | None


class CategoryService:
    def __init__(self, db: AsyncSession, user: User) -> None:
        self.db = db
        self.user = user

    async def create(self, name: str, monthly_budget: Decimal | None) -> Category:
        category = Category(user_id=self.user.id, name=name.strip(), monthly_budget=monthly_budget)
        self.db.add(category)
        try:
            await self.db.commit()
        except IntegrityError:
            # uq_categories_user_id_lower_name: same name (case-insensitive) for this
            # user. The constraint, not a pre-check, decides — no race window.
            await self.db.rollback()
            raise Conflict(
                "A category with this name already exists", code="category_exists"
            ) from None
        return category

    async def list_with_spend(self, today: date | None = None) -> list[CategoryWithSpend]:
        """My categories, each with this month's spend vs budget. "This month" is the
        current calendar month in UTC, the same clock the exchange rates use."""
        current_month = month_start(today or datetime.now(UTC).date())
        result = await self.db.execute(
            _CATEGORIES_WITH_SPEND_SQL,
            {
                "user_id": self.user.id,
                "base_currency": self.user.base_currency,
                "period_start": current_month,
                "period_end": shift_month(current_month, 1),
            },
        )
        return [CategoryWithSpend(**row) for row in result.mappings()]

    async def get(self, category_id: uuid.UUID) -> Category:
        """The user's category, or 404 — also for another user's category, so its
        existence isn't revealed."""
        category = await self.db.scalar(
            select(Category).where(Category.id == category_id, Category.user_id == self.user.id)
        )
        if category is None:
            raise NotFound("Category not found")
        return category
