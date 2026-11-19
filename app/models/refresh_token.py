import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin


class RefreshToken(CreatedAtMixin, Base):
    """One row per issued refresh token — the server-side memory the brief's auth
    rules need ("kill the old one", "revoke all on reuse", "revoke on logout").

    - The token itself is never stored, only its SHA-256: a database leak doesn't
      hand out usable tokens.
    - family_id = one login session. Every rotation adds a row to the same family;
      exactly one row per family is "live" (not used, not revoked) at a time.
    - Access tokens carry the family_id as "sid" and are only accepted while their
      family still has a live row, so revoking refresh tokens also kills the
      matching access tokens immediately.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        # get_current_user runs on every authenticated request:
        #   WHERE family_id = :sid AND used_at IS NULL AND revoked_at IS NULL
        # Partial index: only live rows (one per session), so it stays tiny however
        # many rotations have happened.
        Index(
            "ix_refresh_tokens_live_family_id",
            "family_id",
            postgresql_where=text("used_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    # Index: reuse detection revokes every token of the user (WHERE user_id = ?).
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Unique (so indexed): the lookup on every /auth/refresh and /auth/logout.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set when the token is rotated. Presenting a token with used_at set = reuse = theft.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set by logout or by a reuse cascade.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
