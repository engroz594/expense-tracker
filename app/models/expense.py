import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, currency_check


class Expense(CreatedAtMixin, Base):
    __tablename__ = "expenses"
    __table_args__ = (
        # (category_id, user_id) must match a category owned by the same user — the
        # database itself refuses cross-user references, not just the service layer.
        ForeignKeyConstraint(
            ["category_id", "user_id"],
            ["categories.id", "categories.user_id"],
            name="fk_expenses_category_id_user_id_categories",
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(currency_check("currency"), name="currency"),
        CheckConstraint("version >= 1", name="version_positive"),
        # GET /expenses: WHERE user_id = ? [AND (spent_on, id) < cursor]
        # ORDER BY spent_on DESC, id DESC. Matches the sort exactly, so Postgres walks
        # the index and stops after LIMIT rows — no sort step, no offset scan.
        Index(
            "ix_expenses_user_id_spent_on_id",
            "user_id",
            text("spent_on DESC"),
            text("id DESC"),
        ),
        # Monthly report / category spend / budget alerts:
        # WHERE user_id = ? AND category_id = ? AND spent_on in a month range.
        Index("ix_expenses_user_id_category_id_spent_on", "user_id", "category_id", "spent_on"),
        # q filter: description ILIKE '%term%'. A B-tree can't serve a leading
        # wildcard; a trigram GIN index can.
        Index(
            "ix_expenses_description_trgm",
            "description",
            postgresql_using="gin",
            postgresql_ops={"description": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, server_default="")
    spent_on: Mapped[date] = mapped_column(Date, nullable=False)
    receipt_key: Mapped[str | None] = mapped_column(String(512))
    # Optimistic locking: ETag = version; PATCH bumps it in the same UPDATE ... WHERE
    # version = :expected statement.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
