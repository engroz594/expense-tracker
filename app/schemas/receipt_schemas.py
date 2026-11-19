from pydantic import BaseModel, ConfigDict, Field

from app.enums import ReceiptType


class ReceiptUploadUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: ReceiptType  # image/jpeg, image/png or application/pdf


class ReceiptUploadUrlResponse(BaseModel):
    """POST a multipart form to `url` with every entry of `fields`, then the file as
    the last field named "file". Then call .../receipt/confirm with `key`."""

    url: str
    fields: dict[str, str]
    key: str
    max_bytes: int
    expires_in: int


class ReceiptConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=512)


class ReceiptDownloadResponse(BaseModel):
    url: str
    expires_in: int
