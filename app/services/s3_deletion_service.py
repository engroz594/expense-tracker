"""Deleting S3 objects so that a failure is never forgotten (brief 2.5).

Two steps, split around the commit:
  1. stage(key)      — inside the caller's transaction: insert a pending_s3_deletions
                       row. It commits together with the change that dropped the
                       reference (expense deleted, receipt replaced), or not at all.
  2. delete_now(key) — after the commit: try the S3 delete; on success remove the
                       row, on failure record the error and schedule a retry.

retry_due() is what app/scripts/cleanup_s3_deletions.py runs: every row whose
next_attempt_at has passed is retried, with exponential backoff between attempts.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.storage_client import StorageClient
from app.core.config import settings
from app.models import PendingS3Deletion

logger = logging.getLogger(__name__)


def retry_delay(attempts: int) -> timedelta:
    seconds = settings.S3_DELETE_RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0)
    return timedelta(seconds=min(seconds, settings.S3_DELETE_RETRY_MAX_SECONDS))


class S3DeletionService:
    def __init__(self, db: AsyncSession, storage: StorageClient) -> None:
        self.db = db
        self.storage = storage

    async def stage(self, key: str) -> None:
        """Record the key in the current transaction. The caller commits."""
        await self.db.execute(
            insert(PendingS3Deletion)
            .values(object_key=key)
            .on_conflict_do_nothing(index_elements=["object_key"])
        )

    async def delete_now(self, key: str) -> bool:
        """Try the S3 delete for a committed pending row. True if it's gone."""
        try:
            await self.storage.delete(key)
        except Exception as exc:  # any storage failure: keep the row for a retry
            logger.warning("S3 delete of %s failed; will retry", key, exc_info=True)
            await self._schedule_retry(key, exc)
            return False
        await self.db.execute(delete(PendingS3Deletion).where(PendingS3Deletion.object_key == key))
        await self.db.commit()
        return True

    async def retry_due(self, batch_size: int = 100) -> tuple[int, int]:
        """Retry every deletion that is due. Returns (deleted, still_failing)."""
        deleted = failed = 0
        while True:
            # SKIP LOCKED: two cleanup runs at once never retry the same row.
            keys = list(
                await self.db.scalars(
                    select(PendingS3Deletion.object_key)
                    .where(PendingS3Deletion.next_attempt_at <= datetime.now(UTC))
                    .order_by(PendingS3Deletion.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            if not keys:
                return deleted, failed
            for key in keys:
                if await self.delete_now(key):
                    deleted += 1
                else:
                    failed += 1

    async def _schedule_retry(self, key: str, exc: Exception) -> None:
        row = await self.db.scalar(
            select(PendingS3Deletion).where(PendingS3Deletion.object_key == key)
        )
        if row is None:
            return
        row.attempts += 1
        row.last_error = f"{type(exc).__name__}: {exc}"[:1000]
        row.next_attempt_at = datetime.now(UTC) + retry_delay(row.attempts)
        await self.db.commit()
