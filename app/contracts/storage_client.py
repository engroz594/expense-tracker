"""The file-storage contract. Code that stores files (receipts) depends on this
interface, never on S3 directly, so the backend can be swapped (S3, MinIO, a fake in
tests) without touching callers.

Every method is a coroutine: the app must never block the event loop on network or
disk I/O.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import IO


@dataclass(frozen=True)
class PresignedUpload:
    """A pre-signed POST: the client sends a multipart form with `fields` plus the
    file to `url`, straight to storage, never through the API."""

    url: str
    fields: dict[str, str]


@dataclass(frozen=True)
class ObjectInfo:
    size: int
    content_type: str | None


class StorageClient(ABC):
    @abstractmethod
    async def upload_fileobj(self, fileobj: IO[bytes], key: str, content_type: str) -> None:
        """Store the file object's contents under `key`, streaming it (not read into memory)."""

    @abstractmethod
    async def download_to_file(self, key: str, destination: IO[bytes]) -> None:
        """Stream the object at `key` into `destination`. Raises ObjectNotFound."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete the object at `key`. Deleting a missing key is not an error."""

    @abstractmethod
    async def presign_upload(
        self, key: str, content_type: str, max_bytes: int, expires_in: int
    ) -> PresignedUpload:
        """A pre-signed POST for exactly this key, this content type and at most
        max_bytes — the size limit is enforced by storage, not trusted to the client."""

    @abstractmethod
    async def head(self, key: str) -> ObjectInfo:
        """Size and content type of the object. Raises ObjectNotFound."""

    @abstractmethod
    async def read_first_bytes(self, key: str, count: int) -> bytes:
        """The first `count` bytes of the object (a ranged read). Raises ObjectNotFound."""

    @abstractmethod
    async def presign_download(self, key: str, expires_in: int) -> str:
        """A pre-signed GET URL valid for expires_in seconds."""
