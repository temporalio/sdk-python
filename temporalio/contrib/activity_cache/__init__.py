"""Content-addressed activity memoization with shared remote storage.

This package provides activity-level caching for Temporal workflows. Cached
activities return stored results on repeated calls with the same inputs,
making workflows idempotent across retries, re-runs, and multi-worker
deployments.

The cache is stored remotely (GCS, S3, Azure, local, etc.) via `fsspec`_,
so it is shared across all workers.

.. _fsspec: https://filesystem-spec.readthedocs.io/
"""

from temporalio.contrib.activity_cache._decorator import cached, no_cache
from temporalio.contrib.activity_cache._interceptor import CachingInterceptor
from temporalio.contrib.activity_cache._serializers import register_serializer

__all__ = [
    "CachingInterceptor",
    "cached",
    "no_cache",
    "register_serializer",
]
