"""Retry S3 deletions that failed earlier (brief 2.5: cleaned up later, not forgotten).

    uv run python -m app.scripts.cleanup_s3_deletions

Safe to run any time and concurrently (rows are claimed with FOR UPDATE SKIP
LOCKED). Run it on a schedule: cron locally, an EventBridge-scheduled ECS task on AWS.
Rows not yet due (backoff) are left for a later run.
"""

import asyncio

from app.clients.storage.s3_storage_client import get_storage_client
from app.core.database import SessionLocal, engine
from app.services.s3_deletion_service import S3DeletionService


async def main() -> None:
    try:
        async with SessionLocal() as session:
            deleted, failed = await S3DeletionService(session, get_storage_client()).retry_due()
    finally:
        await engine.dispose()
    print(f"S3 cleanup: {deleted} deleted, {failed} still failing (will retry later).")


if __name__ == "__main__":
    asyncio.run(main())
