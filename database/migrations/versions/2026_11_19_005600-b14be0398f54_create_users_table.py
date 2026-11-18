"""create users table

Revision ID: b14be0398f54
Revises:
Create Date: 2026-11-19 00:56:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b14be0398f54"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        # gen_random_uuid() is built into Postgres 13+; the ORM also sets a uuid4
        # client-side, the server default covers bulk Core inserts.
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "base_currency IN ('PKR', 'AED', 'USD', 'EUR')", name=op.f("ck_users_base_currency")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        # Emails are stored lower-cased by the service, so a plain unique constraint
        # (and its index) serves both duplicate detection (409) and login lookup.
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )


def downgrade() -> None:
    op.drop_table("users")
