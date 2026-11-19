"""The file-storage contract. Code that stores files (CSV imports now, receipts later)
depends on this interface, never on S3 directly, so the backend can be swapped (S3,
MinIO, a local fake in tests) without touching callers.

Every method is a coroutine: the app must never block the event loop on network or
disk I/O.
"""

from abc import ABC, abstractmethod
from typing import IO


class ObjectNotFound(Exception):
    """The requested object doesn't exist (or isn't visible to us)."""


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
