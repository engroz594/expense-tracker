import io

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.storage.s3_storage_client import S3StorageClient
from app.models import PendingS3Deletion
from app.scripts import cleanup_s3_deletions


@pytest.mark.usefixtures("s3_bucket")
async def test_script_deletes_due_files_and_reports(db: AsyncSession, capsys):
    storage = S3StorageClient()
    await storage.upload_fileobj(io.BytesIO(b"%PDF-1.7"), "receipts/u/e/a.pdf", "application/pdf")
    db.add(PendingS3Deletion(object_key="receipts/u/e/a.pdf"))
    await db.commit()

    await cleanup_s3_deletions.main()

    assert "1 deleted, 0 still failing" in capsys.readouterr().out
    assert await db.scalar(select(PendingS3Deletion)) is None
