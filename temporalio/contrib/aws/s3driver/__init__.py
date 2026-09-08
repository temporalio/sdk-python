"""Amazon S3 storage driver for Temporal external storage.

.. warning::
    This API is experimental.
"""

from temporalio.contrib.aws.s3driver._client import S3StorageDriverClient
from temporalio.contrib.aws.s3driver._driver import S3StorageDriver

__all__ = [
    "S3StorageDriverClient",
    "S3StorageDriver",
]
