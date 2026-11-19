"""S3StorageClient against moto's fake S3 server (never real S3)."""

import io

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from app.clients.storage.s3_storage_client import S3StorageClient, get_storage_client
from app.contracts.storage_client import ObjectNotFound, StorageClient
from app.core.config import settings


@pytest.fixture
def storage(s3_bucket: str) -> S3StorageClient:
    return S3StorageClient()


async def content_type_of(storage: S3StorageClient, key: str) -> str:
    async with storage._client() as client:
        return (await client.head_object(Bucket=settings.S3_BUCKET, Key=key))["ContentType"]


def test_s3_client_implements_the_storage_contract():
    assert isinstance(get_storage_client(), StorageClient)
    # The contract can't be used on its own: every method is abstract.
    with pytest.raises(TypeError):
        StorageClient()  # type: ignore[abstract]


async def test_upload_then_download_round_trip(storage: S3StorageClient):
    # Larger than one download chunk, so the streaming loop runs more than once.
    content = b"date,amount,currency,category\n2026-11-01,10.00,PKR,Food\n" * 40_000
    assert len(content) > 2 * storage.DOWNLOAD_CHUNK_BYTES

    await storage.upload_fileobj(io.BytesIO(content), "imports/u1/file.csv", "text/csv")
    destination = io.BytesIO()
    await storage.download_to_file("imports/u1/file.csv", destination)

    assert destination.getvalue() == content
    assert await content_type_of(storage, "imports/u1/file.csv") == "text/csv"


async def test_downloading_a_missing_key_raises_object_not_found(storage: S3StorageClient):
    with pytest.raises(ObjectNotFound):
        await storage.download_to_file("imports/does-not-exist.csv", io.BytesIO())


async def test_delete_removes_the_object(storage: S3StorageClient):
    await storage.upload_fileobj(io.BytesIO(b"x"), "receipts/k.png", "image/png")

    await storage.delete("receipts/k.png")

    with pytest.raises(ObjectNotFound):
        await storage.download_to_file("receipts/k.png", io.BytesIO())


async def test_deleting_a_missing_key_is_not_an_error(storage: S3StorageClient):
    await storage.delete("receipts/never-existed.png")  # S3 delete is idempotent


async def test_other_s3_errors_are_not_swallowed(storage: S3StorageClient, monkeypatch):
    monkeypatch.setattr(settings, "S3_BUCKET", "bucket-that-does-not-exist")

    with pytest.raises(ClientError) as exc_info:
        await storage.download_to_file("any.csv", io.BytesIO())

    assert exc_info.value.response["Error"]["Code"] == "NoSuchBucket"


def test_explicit_keys_are_used_only_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "S3_ACCESS_KEY_ID", SecretStr("AKIA-test"))
    monkeypatch.setattr(settings, "S3_SECRET_ACCESS_KEY", SecretStr("secret-test"))
    with_keys = S3StorageClient._client_kwargs()

    monkeypatch.setattr(settings, "S3_ACCESS_KEY_ID", None)
    monkeypatch.setattr(settings, "S3_SECRET_ACCESS_KEY", None)
    without_keys = S3StorageClient._client_kwargs()

    assert with_keys["aws_access_key_id"] == "AKIA-test"
    # No keys configured -> botocore's default chain (on ECS: the task's IAM role).
    assert "aws_access_key_id" not in without_keys
    assert "aws_secret_access_key" not in without_keys
