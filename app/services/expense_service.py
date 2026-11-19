"""Expense writes and reads.

Every query filters on user_id, so another user's expense simply doesn't exist for
the caller (404, never 403).

Writes are built as "stage, then commit": _stage_create adds the expense to the
session without committing. Later slices add their rows (the outbox event) to the
same transaction before the single commit, so the expense and its side records are
always written together or not at all.
"""

import uuid

from sqlalchemy import delete, exists, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.exceptions import NotFound, PreconditionFailed
from app.models import Expense, User
from app.schemas.expense_schemas import ExpenseCreate, ExpenseListQuery, ExpenseUpdate
from app.services.category_service import CategoryService
from app.services.currency_service import ensure_rates_available


def _escape_like(term: str) -> str:
    # The search term is data, not a pattern: a user typing "50%" or "a_b" must
    # match those characters literally, not act as wildcards.
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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

    async def list(self, query: ExpenseListQuery) -> tuple[list[Expense], str | None]:
        """One page of my expenses, newest first, plus the cursor for the next page.

        Keyset pagination: ORDER BY spent_on DESC, id DESC, and the next page is
        WHERE (spent_on, id) < (cursor's spent_on, cursor's id). id breaks ties
        between expenses on the same day, so the order is total and stable.
        Served by ix_expenses_user_id_spent_on_id, which has exactly this order:
        Postgres walks the index from the cursor and stops after limit + 1 rows.
        """
        stmt = select(Expense).where(Expense.user_id == self.user.id)
        if query.category_id is not None:
            stmt = stmt.where(Expense.category_id == query.category_id)
        if query.date_from is not None:
            stmt = stmt.where(Expense.spent_on >= query.date_from)
        if query.date_to is not None:
            stmt = stmt.where(Expense.spent_on <= query.date_to)
        if query.min_amount is not None:
            stmt = stmt.where(Expense.amount >= query.min_amount)
        if query.max_amount is not None:
            stmt = stmt.where(Expense.amount <= query.max_amount)
        if query.currency is not None:
            stmt = stmt.where(Expense.currency == query.currency.value)
        if query.q is not None:
            # Served by the trigram GIN index (ix_expenses_description_trgm).
            stmt = stmt.where(Expense.description.ilike(f"%{_escape_like(query.q)}%", escape="\\"))
        if query.cursor is not None:
            after_spent_on, after_id = decode_cursor(query.cursor)
            stmt = stmt.where(tuple_(Expense.spent_on, Expense.id) < (after_spent_on, after_id))

        # Fetch one extra row: if it exists there is a next page — no COUNT(*) needed.
        stmt = stmt.order_by(Expense.spent_on.desc(), Expense.id.desc()).limit(query.limit + 1)
        rows = list(await self.db.scalars(stmt))
        page = rows[: query.limit]
        next_cursor = (
            encode_cursor(page[-1].spent_on, page[-1].id) if len(rows) > query.limit else None
        )
        return page, next_cursor

    async def update(
        self, expense_id: uuid.UUID, expected_version: int, data: ExpenseUpdate
    ) -> Expense:
        """Apply a PATCH only if the expense is still at expected_version.

        The version check and the write are ONE statement:
            UPDATE expenses SET ..., version = version + 1
            WHERE id = :id AND user_id = :user AND version = :expected
            RETURNING *
        Two clients sending the same ETag can't both win: Postgres locks the row for
        the first UPDATE; the second waits, then re-checks its WHERE against the
        committed row, finds version already bumped, and matches nothing -> 412.

        The SELECT below is NOT part of the concurrency check — the version is never
        compared in Python. It only provides 404-if-not-mine and the current values
        needed to validate a partial update (e.g. a new currency with the old date).
        """
        current = await self.get(expense_id)
        changes = data.changes()

        if "category_id" in changes:
            await CategoryService(self.db, self.user).get(changes["category_id"])
        if "currency" in changes or "spent_on" in changes:
            currency = changes.get("currency", current.currency)
            await ensure_rates_available(
                self.db,
                {str(currency), self.user.base_currency},
                changes.get("spent_on", current.spent_on),
            )
        if "currency" in changes:
            changes["currency"] = str(changes["currency"])

        updated = await self.db.scalar(
            update(Expense)
            .where(
                Expense.id == expense_id,
                Expense.user_id == self.user.id,
                Expense.version == expected_version,
            )
            .values(**changes, version=Expense.version + 1, updated_at=func.now())
            .returning(Expense)
            # Refresh the already-loaded `current` object with the returned row.
            .execution_options(populate_existing=True)
        )
        if updated is None:
            # Zero rows matched — not an error, so the transaction is still usable and
            # nothing needs rolling back. (A rollback would also expire every loaded
            # object, including self.user, whose next attribute access would then
            # trigger a lazy load that async SQLAlchemy forbids.)
            # It existed a moment ago; if it still does, the version moved on.
            still_there = await self.db.scalar(
                select(exists().where(Expense.id == expense_id, Expense.user_id == self.user.id))
            )
            if not still_there:
                raise NotFound("Expense not found")
            raise PreconditionFailed(
                "The expense was modified by someone else; fetch it again and retry",
                code="version_conflict",
            )
        await self.db.commit()
        return updated

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
