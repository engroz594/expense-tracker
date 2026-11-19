import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.storage.s3_storage_client import get_storage_client
from app.contracts.storage_client import StorageClient
from app.controllers.api.v1.expense_controller import etag_for
from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.models import User
from app.schemas.expense_schemas import ExpenseResponse
from app.schemas.receipt_schemas import (
    ReceiptConfirmRequest,
    ReceiptDownloadResponse,
    ReceiptUploadUrlRequest,
    ReceiptUploadUrlResponse,
)
from app.services.receipt_service import ReceiptService

router = APIRouter()


def get_receipt_service(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    storage: StorageClient = Depends(get_storage_client),
) -> ReceiptService:
    return ReceiptService(db, user, storage)


@router.post("/{expense_id}/receipt/upload-url", response_model=ReceiptUploadUrlResponse)
async def receipt_upload_url(
    expense_id: uuid.UUID,
    payload: ReceiptUploadUrlRequest,
    receipts: ReceiptService = Depends(get_receipt_service),
) -> ReceiptUploadUrlResponse:
    key, upload = await receipts.upload_url(expense_id, payload.content_type)
    return ReceiptUploadUrlResponse(
        url=upload.url,
        fields=upload.fields,
        key=key,
        max_bytes=settings.RECEIPT_MAX_BYTES,
        expires_in=settings.RECEIPT_UPLOAD_URL_TTL_SECONDS,
    )


@router.post("/{expense_id}/receipt/confirm", response_model=ExpenseResponse)
async def receipt_confirm(
    expense_id: uuid.UUID,
    payload: ReceiptConfirmRequest,
    response: Response,
    receipts: ReceiptService = Depends(get_receipt_service),
) -> ExpenseResponse:
    expense = await receipts.confirm(expense_id, payload.key)
    response.headers["ETag"] = etag_for(expense.version)
    return ExpenseResponse.model_validate(expense)


@router.get("/{expense_id}/receipt", response_model=ReceiptDownloadResponse)
async def receipt_download(
    expense_id: uuid.UUID, receipts: ReceiptService = Depends(get_receipt_service)
) -> ReceiptDownloadResponse:
    url = await receipts.download_url(expense_id)
    return ReceiptDownloadResponse(url=url, expires_in=settings.RECEIPT_DOWNLOAD_URL_TTL_SECONDS)
