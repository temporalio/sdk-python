"""S3 storage driver client abstraction for the S3 storage driver.

.. warning::
    This API is experimental.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class S3StorageDriverClient(ABC):
    """Abstract base class for S3 object operations.

    Implementations must support ``put_object`` and ``get_object``. Multipart
    upload handling (if needed) is an internal concern of each implementation.

    .. warning::
        This API is experimental.
    """

    @abstractmethod
    async def put_object(self, *, bucket: str, key: str, data: bytes) -> None:
        """Upload *data* to the given S3 *bucket* and *key*."""

    @abstractmethod
    async def object_exists(self, *, bucket: str, key: str) -> bool:
        """Return ``True`` if an object exists at the given *bucket* and *key*."""

    @abstractmethod
    async def get_object(self, *, bucket: str, key: str) -> bytes:
        """Download and return the bytes stored at the given S3 *bucket* and *key*."""
