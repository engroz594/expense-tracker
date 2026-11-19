import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import Conflict, NotFound
from app.models import Category, User


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

    async def list(self) -> Sequence[Category]:
        result = await self.db.scalars(
            select(Category).where(Category.user_id == self.user.id).order_by(Category.name)
        )
        return result.all()

    async def get(self, category_id: uuid.UUID) -> Category:
        """The user's category, or 404 — also for another user's category, so its
        existence isn't revealed."""
        category = await self.db.scalar(
            select(Category).where(Category.id == category_id, Category.user_id == self.user.id)
        )
        if category is None:
            raise NotFound("Category not found")
        return category
