"""GCS storage driver client abstraction for the GCS storage driver.

.. warning::
    This API is experimental.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping


class GCSStorageDriverClient(ABC):
    """Abstract base class for GCS object operations.

    Implementations must support ``object_exists``, ``put_object``, and
    ``get_object``. Resumable uploads (if needed) are an internal concern of
    each implementation. Uploads must leave existing objects unchanged.

    .. warning::
        This API is experimental.
    """

    @abstractmethod
    async def put_object(self, *, bucket: str, object_name: str, data: bytes) -> None:
        """Upload *data* if absent at the given GCS *bucket* and *object_name*."""

    @abstractmethod
    async def object_exists(self, *, bucket: str, object_name: str) -> bool:
        """Return ``True`` if an object exists at the given *bucket* and *object_name*."""

    @abstractmethod
    async def get_object(self, *, bucket: str, object_name: str) -> bytes:
        """Download and return the bytes stored at the given GCS *bucket* and *object_name*."""

    def describe(self) -> Mapping[str, str]:
        """Return client-specific diagnostic metadata (e.g. region, credentials
        source) that the driver appends to error messages. Implementations may
        override this to surface configuration that is useful for debugging
        common misconfigurations. Returns an empty mapping by default.
        """
        return {}
