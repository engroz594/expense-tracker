"""Expense writes and reads.

Every query filters on user_id, so another user's expense simply doesn't exist for
the caller (404, never 403).

Writes are built as "stage, then commit": _stage_create adds the expense to the
session without committing. Later slices add their rows (idempotency record, outbox
event) to the same transaction before the single commit, so the expense and its
side records are always written together or not at all.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import NotFound
from app.models import Expense, User
from app.schemas.expense_schemas import ExpenseCreate
from app.services.category_service import CategoryService
from app.services.currency_service import ensure_rates_available


class ExpenseService:
    def __init__(self, db: AsyncSession, user: User) -> None:
        self.db = db
        self.user = user

    async def create(self, data: ExpenseCreate) -> Expense:
        expense = await self._stage_create(data)
        await self.db.commit()
        return expense

    async def get(self, expense_id: uuid.UUID) -> Expense:
        expense = await self.db.scalar(
            select(Expense).where(Expense.id == expense_id, Expense.user_id == self.user.id)
        )
        if expense is None:
            raise NotFound("Expense not found")
        return expense

    async def delete(self, expense_id: uuid.UUID) -> None:
        # One statement: the ownership check is part of the WHERE, so there is no
        # read-then-delete gap.
        deleted = await self.db.scalar(
            delete(Expense)
            .where(Expense.id == expense_id, Expense.user_id == self.user.id)
            .returning(Expense.id)
        )
        if deleted is None:
            raise NotFound("Expense not found")
        await self.db.commit()

    async def _stage_create(self, data: ExpenseCreate) -> Expense:
        await CategoryService(self.db, self.user).get(data.category_id)  # 404 if not mine
        await ensure_rates_available(
            self.db, {data.currency.value, self.user.base_currency}, data.spent_on
        )
        expense = Expense(
            user_id=self.user.id,
            category_id=data.category_id,
            amount=data.amount,
            currency=data.currency.value,
            description=data.description,
            spent_on=data.spent_on,
        )
        self.db.add(expense)
        # Flush (not commit) so server defaults (version, timestamps) are loaded and
        # constraint errors surface here, inside the still-open transaction.
        await self.db.flush()
        await self.db.refresh(expense)
        return expense
