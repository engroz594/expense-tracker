import uuid
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin


class Category(CreatedAtMixin, Base):
    __tablename__ = "categories"
    __table_args__ = (
        # Duplicate names per user -> 409. Case-insensitive ("Food" == "food"), so it is
        # an expression index on lower(name), not a plain unique constraint.
        Index("uq_categories_user_id_lower_name", "user_id", text("lower(name)"), unique=True),
        # Target of the composite FK from expenses: guarantees at the database level
        # that an expense can only point at a category owned by the same user.
        UniqueConstraint("id", "user_id"),
        CheckConstraint("monthly_budget > 0", name="monthly_budget_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # In the owner's base currency. NULL = no budget, so no alerts.
    monthly_budget: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
