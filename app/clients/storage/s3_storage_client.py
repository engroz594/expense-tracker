"""S3 implementation of StorageClient — all S3 code lives in this module (brief 2.5).

Built on aiobotocore, the asyncio version of botocore, so no S3 call blocks the event
loop. Local file I/O around it (writing a download to disk) goes through
asyncio.to_thread.

Credentials: explicit keys only when configured (MinIO/LocalStack/moto). Otherwise
botocore's default credential chain is used, which on ECS is the task's IAM role,
so production config holds no AWS keys.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import IO, Any

from aiobotocore.session import get_session
from botocore.exceptions import ClientError

from app.contracts.storage_client import ObjectNotFound, StorageClient
from app.core.config import settings

# Without s3:ListBucket (the app's IAM policy doesn't grant it) S3 answers 403, not
# 404, for a missing key — both mean "not there" to us.
_MISSING_CODES = {"NoSuchKey", "404", "403", "NotFound", "AccessDenied"}


class S3StorageClient(StorageClient):
    DOWNLOAD_CHUNK_BYTES = 1024 * 1024

    # One aiobotocore session per process (it caches the loaded S3 service model).
    _session = get_session()

    @staticmethod
    def _client_kwargs() -> dict[str, Any]:
        # Read settings at call time, so tests can point the client at a moto server.
        kwargs: dict[str, Any] = {
            "region_name": settings.S3_REGION,
            "endpoint_url": settings.S3_ENDPOINT_URL,
        }
        if settings.S3_ACCESS_KEY_ID and settings.S3_SECRET_ACCESS_KEY:
            kwargs["aws_access_key_id"] = settings.S3_ACCESS_KEY_ID.get_secret_value()
            kwargs["aws_secret_access_key"] = settings.S3_SECRET_ACCESS_KEY.get_secret_value()
        return kwargs

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        async with self._session.create_client("s3", **self._client_kwargs()) as client:
            yield client

    async def upload_fileobj(self, fileobj: IO[bytes], key: str, content_type: str) -> None:
        async with self._client() as client:
            await client.put_object(
                Bucket=settings.S3_BUCKET, Key=key, Body=fileobj, ContentType=content_type
            )

    async def download_to_file(self, key: str, destination: IO[bytes]) -> None:
        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=settings.S3_BUCKET, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in _MISSING_CODES:
                    raise ObjectNotFound(key) from exc
                raise
            body = response["Body"]
            try:
                async for chunk in body.iter_chunks(self.DOWNLOAD_CHUNK_BYTES):
                    await asyncio.to_thread(destination.write, chunk)
            finally:
                body.close()
        await asyncio.to_thread(destination.flush)

    async def delete(self, key: str) -> None:
        async with self._client() as client:
            await client.delete_object(Bucket=settings.S3_BUCKET, Key=key)


def get_storage_client() -> StorageClient:
    """FastAPI dependency (and the worker's factory): callers get the interface, so
    tests can override it with another StorageClient."""
    return S3StorageClient()
