"""Gcloud-aio-storage adapter for the GCS storage driver client.

.. warning::
    This API is experimental.
"""

from __future__ import annotations

from aiohttp import ClientResponseError
from gcloud.aio.storage import Storage

from temporalio.contrib.gcp.gcsdriver._client import GCSStorageDriverClient


class _GcloudAioStorageDriverClient(GCSStorageDriverClient):
    """Adapter for an application-owned gcloud-aio-storage client."""

    def __init__(self, client: Storage) -> None:
        """Wrap the async storage client."""
        self._client = client

    async def object_exists(self, *, bucket: str, object_name: str) -> bool:
        """Check existence via object metadata."""
        try:
            await self._client.download_metadata(bucket, object_name)
            return True
        except ClientResponseError as err:
            if err.status == 404:
                return False
            raise

    async def put_object(self, *, bucket: str, object_name: str, data: bytes) -> None:
        """Upload bytes using conditional object creation."""
        try:
            await self._client.upload(
                bucket,
                object_name,
                data,
                content_type="application/octet-stream",
                parameters={"ifGenerationMatch": "0"},
                force_resumable_upload=False,
            )
        except ClientResponseError as err:
            # Another writer may create the content-addressed object after our check.
            if err.status == 412:
                return
            raise

    async def get_object(self, *, bucket: str, object_name: str) -> bytes:
        """Download the object's bytes."""
        return await self._client.download(bucket, object_name)


def new_gcloud_aio_client(client: Storage) -> GCSStorageDriverClient:
    """Create a GCS storage driver client from a gcloud-aio-storage client.

    Args:
        client: An application-owned ``gcloud.aio.storage.Storage`` client. Keep
            it open until all Temporal clients and workers using it have stopped.

    .. warning::
        This API is experimental.
    """
    return _GcloudAioStorageDriverClient(client)
