"""create refresh_tokens table

Server-side state for the brief's auth rules: rotation kills the old token, reusing
an old token revokes all of the user's tokens, logout revokes the current one.

Revision ID: 02015a20f67d
Revises: 7fb872c0af0e
Create Date: 2026-11-19 11:31:08.803761

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "02015a20f67d"
down_revision: str | Sequence[str] | None = "7fb872c0af0e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # One login session; rotations stay in the same family.
        sa.Column("family_id", sa.Uuid(), nullable=False),
        # SHA-256 of the token; the token itself is never stored.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        # Unique (so indexed): the lookup on every /auth/refresh and /auth/logout.
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    # Every authenticated request checks its session is still live:
    #   WHERE family_id = :sid AND used_at IS NULL AND revoked_at IS NULL
    # Partial: only live rows (one per session), so it stays tiny.
    op.create_index(
        "ix_refresh_tokens_live_family_id",
        "refresh_tokens",
        ["family_id"],
        unique=False,
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )
    # Reuse detection revokes every token of the user: WHERE user_id = ?
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_live_family_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
