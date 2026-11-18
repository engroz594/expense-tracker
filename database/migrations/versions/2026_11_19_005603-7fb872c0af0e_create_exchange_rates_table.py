"""create exchange_rates table

Revision ID: 7fb872c0af0e
Revises: f41a71aa979e
Create Date: 2026-11-19 00:56:03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7fb872c0af0e"
down_revision: str | Sequence[str] | None = "f41a71aa979e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exchange_rates",
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("valid_on", sa.Date(), nullable=False),
        # USD value of one unit of `currency` on `valid_on`.
        sa.Column("rate_to_usd", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.CheckConstraint(
            "currency IN ('PKR', 'AED', 'USD', 'EUR')", name=op.f("ck_exchange_rates_currency")
        ),
        sa.CheckConstraint("rate_to_usd > 0", name=op.f("ck_exchange_rates_rate_positive")),
        # One row per currency per day. The PK index also serves the "latest rate on
        # or before d" lookup: WHERE currency = ? AND valid_on <= ?
        # ORDER BY valid_on DESC LIMIT 1 — a single backward index probe.
        sa.PrimaryKeyConstraint("currency", "valid_on", name=op.f("pk_exchange_rates")),
    )


def downgrade() -> None:
    op.drop_table("exchange_rates")
