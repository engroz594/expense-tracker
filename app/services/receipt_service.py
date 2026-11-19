"""Receipts on S3 (brief 2.5).

Flow:
  1. upload-url: the API picks the key receipts/{user}/{expense}/{uuid}.{ext} and
     returns a pre-signed POST limited to that key, that content type and 5 MB.
     The client uploads straight to S3; the file never passes through the API.
  2. confirm:    the API checks the key belongs to this expense, that the object
     really exists (HEAD) and is <= 5 MB, and reads its first bytes to check the
     REAL type (the extension and content type are just claims). On a mismatch the
     object is deleted and the request fails with 422. Otherwise receipt_key is
     saved (version bumped, like any change); a replaced receipt is deleted.
  3. download:   a pre-signed GET valid for 15 minutes.
"""

import uuid

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.storage_client import ObjectNotFound, PresignedUpload, StorageClient
from app.core.config import settings
from app.enums import ReceiptType
from app.exceptions import NotFound, UnprocessableEntity
from app.models import Expense, User
from app.services.expense_service import ExpenseService
from app.services.s3_deletion_service import S3DeletionService

# File signatures ("magic numbers"): the first bytes of each allowed format.
MAGIC_BYTES: dict[ReceiptType, bytes] = {
    ReceiptType.JPEG: b"\xff\xd8\xff",
    ReceiptType.PNG: b"\x89PNG\r\n\x1a\n",
    ReceiptType.PDF: b"%PDF-",
}
_SNIFF_BYTES = max(len(sig) for sig in MAGIC_BYTES.values())


def detect_type(head: bytes) -> ReceiptType | None:
    return next((t for t, sig in MAGIC_BYTES.items() if head.startswith(sig)), None)


class ReceiptService:
    def __init__(self, db: AsyncSession, user: User, storage: StorageClient) -> None:
        self.db = db
        self.user = user
        self.storage = storage
        self.expenses = ExpenseService(db, user)
        self.deletions = S3DeletionService(db, storage)

    def _prefix(self, expense_id: uuid.UUID) -> str:
        return f"receipts/{self.user.id}/{expense_id}/"

    async def upload_url(
        self, expense_id: uuid.UUID, receipt_type: ReceiptType
    ) -> tuple[str, PresignedUpload]:
        await self.expenses.get(expense_id)  # 404 if not mine
        key = f"{self._prefix(expense_id)}{uuid.uuid4()}.{receipt_type.extension}"
        upload = await self.storage.presign_upload(
            key,
            receipt_type.value,
            max_bytes=settings.RECEIPT_MAX_BYTES,
            expires_in=settings.RECEIPT_UPLOAD_URL_TTL_SECONDS,
        )
        return key, upload

    async def confirm(self, expense_id: uuid.UUID, key: str) -> Expense:
        expense = await self.expenses.get(expense_id)  # 404 if not mine
        claimed = self._claimed_type(expense_id, key)

        try:
            info = await self.storage.head(key)
        except ObjectNotFound:
            raise NotFound("No uploaded file at this key", code="receipt_not_uploaded") from None
        if info.size > settings.RECEIPT_MAX_BYTES:
            await self.storage.delete(key)
            raise UnprocessableEntity("Receipt is larger than 5 MB", code="receipt_too_large")
        actual = detect_type(await self.storage.read_first_bytes(key, _SNIFF_BYTES))
        if actual is None or actual != claimed:
            # E.g. an .exe renamed to .png: the bytes don't match. Don't keep it.
            await self.storage.delete(key)
            raise UnprocessableEntity(
                "File content is not a valid JPG, PNG or PDF matching its extension",
                code="invalid_receipt_type",
            )

        previous_key = expense.receipt_key
        updated = await self.db.scalar(
            update(Expense)
            .where(Expense.id == expense_id, Expense.user_id == self.user.id)
            .values(receipt_key=key, version=Expense.version + 1, updated_at=func.now())
            .returning(Expense)
            .execution_options(populate_existing=True)
        )
        replaced = previous_key is not None and previous_key != key
        if replaced:
            await self.deletions.stage(previous_key)  # same transaction as the update
        await self.db.commit()
        if replaced:
            await self.deletions.delete_now(previous_key)
        return updated

    async def download_url(self, expense_id: uuid.UUID) -> str:
        expense = await self.expenses.get(expense_id)
        if not expense.receipt_key:
            raise NotFound("This expense has no receipt", code="no_receipt")
        return await self.storage.presign_download(
            expense.receipt_key, expires_in=settings.RECEIPT_DOWNLOAD_URL_TTL_SECONDS
        )

    def _claimed_type(self, expense_id: uuid.UUID, key: str) -> ReceiptType:
        """The key must be one we could have issued for this expense: right prefix,
        a single uuid file name, an allowed extension. Anything else is refused, so
        a client can't attach another user's (or another expense's) file."""
        prefix = self._prefix(expense_id)
        name = key.removeprefix(prefix) if key.startswith(prefix) else ""
        stem, _, ext = name.rpartition(".")
        try:
            uuid.UUID(stem)
            return ReceiptType.from_extension(ext)
        except ValueError:
            raise UnprocessableEntity(
                "This key was not issued for this expense", code="invalid_receipt_key"
            ) from None
