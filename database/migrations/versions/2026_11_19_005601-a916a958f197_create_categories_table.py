"""create categories table

Revision ID: a916a958f197
Revises: b14be0398f54
Create Date: 2026-11-19 00:56:01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a916a958f197"
down_revision: str | Sequence[str] | None = "b14be0398f54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        # In the owner's base currency. NULL = no budget, so no alerts.
        sa.Column("monthly_budget", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "monthly_budget > 0", name=op.f("ck_categories_monthly_budget_positive")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_categories_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
        # Target of the composite FK from expenses (category_id, user_id): lets the
        # database refuse an expense that points at another user's category.
        sa.UniqueConstraint("id", "user_id", name=op.f("uq_categories_id_user_id")),
    )
    # Duplicate category names per user => 409, case-insensitively ("Food" == "food").
    # An expression index on lower(name), since a plain unique constraint can't do that.
    # Leading user_id also serves "list my categories" and the users FK cascade.
    op.create_index(
        "uq_categories_user_id_lower_name",
        "categories",
        ["user_id", sa.literal_column("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_categories_user_id_lower_name", table_name="categories")
    op.drop_table("categories")
