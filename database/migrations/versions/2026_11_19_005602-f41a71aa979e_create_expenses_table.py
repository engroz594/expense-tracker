"""create expenses table

Revision ID: f41a71aa979e
Revises: a916a958f197
Create Date: 2026-11-19 00:56:02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f41a71aa979e"
down_revision: str | Sequence[str] | None = "a916a958f197"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Needed by ix_expenses_description_trgm below. pg_trgm is a "trusted" extension
    # (PG13+), so the database owner can create it without superuser rights.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "expenses",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        # NUMERIC, never float: exact decimal money.
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("description", sa.String(length=500), server_default="", nullable=False),
        sa.Column("spent_on", sa.Date(), nullable=False),
        sa.Column("receipt_key", sa.String(length=512), nullable=True),
        # Optimistic locking: ETag = version, bumped by every PATCH.
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "currency IN ('PKR', 'AED', 'USD', 'EUR')", name=op.f("ck_expenses_currency")
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_expenses_amount_positive")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_expenses_version_positive")),
        # Composite FK: (category_id, user_id) must be a category of the same user.
        sa.ForeignKeyConstraint(
            ["category_id", "user_id"],
            ["categories.id", "categories.user_id"],
            name="fk_expenses_category_id_user_id_categories",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_expenses_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expenses")),
    )
    # GET /expenses: WHERE user_id = ? [AND (spent_on, id) < cursor]
    # ORDER BY spent_on DESC, id DESC LIMIT n. Same order as the sort, so Postgres
    # reads the index in order and stops after n rows — no sort, no offset scan.
    op.create_index(
        "ix_expenses_user_id_spent_on_id",
        "expenses",
        ["user_id", sa.literal_column("spent_on DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    # Monthly report, category spend, budget alerts: per user + category + month range.
    # Also serves the composite FK check when a category is deleted.
    op.create_index(
        "ix_expenses_user_id_category_id_spent_on",
        "expenses",
        ["user_id", "category_id", "spent_on"],
        unique=False,
    )
    # q filter: description ILIKE '%term%'. A B-tree can't serve a leading wildcard;
    # a trigram GIN index can.
    op.create_index(
        "ix_expenses_description_trgm",
        "expenses",
        ["description"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"description": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_expenses_description_trgm", table_name="expenses")
    op.drop_index("ix_expenses_user_id_category_id_spent_on", table_name="expenses")
    op.drop_index("ix_expenses_user_id_spent_on_id", table_name="expenses")
    op.drop_table("expenses")
    # pg_trgm is deliberately left installed: extensions are database-wide and may be
    # used by objects outside this schema.
