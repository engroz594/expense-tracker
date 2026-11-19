"""S3 implementation of StorageClient — all S3 code lives in this module (brief 2.5).

Built on aiobotocore, the asyncio version of botocore, so no S3 call blocks the event
loop. Local file I/O around it (writing a download to disk) goes through
asyncio.to_thread.

Credentials: explicit keys only when configured (MinIO/LocalStack/moto). Otherwise
botocore's default credential chain is used, which on ECS is the task's IAM role,
so production config holds no AWS keys.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import IO, Any

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import ClientError

from app.contracts.storage_client import ObjectInfo, ObjectNotFound, PresignedUpload, StorageClient
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
            # SigV4 for pre-signed URLs; path-style addressing for custom endpoints
            # (MinIO/moto), where bucket subdomains don't resolve.
            "config": AioConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path" if settings.S3_ENDPOINT_URL else "auto"},
            ),
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
            with _missing_as_not_found(key):
                response = await client.get_object(Bucket=settings.S3_BUCKET, Key=key)
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

    async def presign_upload(
        self, key: str, content_type: str, max_bytes: int, expires_in: int
    ) -> PresignedUpload:
        async with self._client() as client:
            post = await client.generate_presigned_post(
                Bucket=settings.S3_BUCKET,
                Key=key,
                Fields={"Content-Type": content_type},
                # Signed into the policy: S3 itself rejects a different key, a
                # different content type, or a file outside 1..max_bytes. (A
                # pre-signed PUT can't limit the size; a POST policy can.)
                Conditions=[
                    {"Content-Type": content_type},
                    ["content-length-range", 1, max_bytes],
                ],
                ExpiresIn=expires_in,
            )
        return PresignedUpload(url=post["url"], fields=post["fields"])

    async def head(self, key: str) -> ObjectInfo:
        async with self._client() as client:
            with _missing_as_not_found(key):
                response = await client.head_object(Bucket=settings.S3_BUCKET, Key=key)
        return ObjectInfo(size=response["ContentLength"], content_type=response.get("ContentType"))

    async def read_first_bytes(self, key: str, count: int) -> bytes:
        async with self._client() as client:
            with _missing_as_not_found(key):
                response = await client.get_object(
                    Bucket=settings.S3_BUCKET, Key=key, Range=f"bytes=0-{count - 1}"
                )
            async with response["Body"] as body:
                return await body.read()

    async def presign_download(self, key: str, expires_in: int) -> str:
        async with self._client() as client:
            return await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.S3_BUCKET, "Key": key},
                ExpiresIn=expires_in,
            )


@contextmanager
def _missing_as_not_found(key: str) -> Iterator[None]:
    try:
        yield
    except ClientError as exc:
        if exc.response["Error"]["Code"] in _MISSING_CODES:
            raise ObjectNotFound(key) from exc
        raise


def get_storage_client() -> StorageClient:
    """FastAPI dependency (and the worker's factory): callers get the interface, so
    tests can override it with another StorageClient."""
    return S3StorageClient()
