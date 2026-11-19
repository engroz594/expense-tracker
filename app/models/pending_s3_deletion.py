from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin


class PendingS3Deletion(CreatedAtMixin, Base):
    """An S3 object that must be deleted (brief 2.5: a file whose S3 delete failed
    must be cleaned up later, not forgotten).

    The row is written in the SAME transaction that removes the database reference
    (expense deleted, receipt replaced), so it exists if and only if that change
    committed. After the commit the API tries the S3 delete straight away and removes
    the row on success; if S3 fails, the row stays and
    app/scripts/cleanup_s3_deletions.py retries it with backoff.
    """

    __tablename__ = "pending_s3_deletions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    object_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Index: the cleanup script picks WHERE next_attempt_at <= now().
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text)
