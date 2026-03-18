"""Temporal ActivityInboundInterceptor for transparent activity caching."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import temporalio.worker

from temporalio.contrib.activity_cache._decorator import NO_CACHE_ATTR
from temporalio.contrib.activity_cache._keys import compute_cache_key
from temporalio.contrib.activity_cache._store import CacheStore

logger = logging.getLogger(__name__)


class CachingInterceptor(temporalio.worker.Interceptor):
    """Temporal interceptor that caches all activity results.

    When added to a worker's interceptor list, all activities are
    automatically cached. Use :func:`~temporalio.contrib.activity_cache.no_cache`
    to opt out specific activities.

    Args:
        store_url: Base URL for the cache store.
        ttl: Optional default TTL for all cached entries.
        key_fn: Optional function to compute cache keys from activity args.
            If not provided, all arguments are included in the key.
        **storage_options: Extra keyword arguments passed to the fsspec
            filesystem.

    Example::

        from temporalio.contrib.activity_cache import CachingInterceptor

        worker = Worker(
            client,
            task_queue="my-queue",
            activities=[extract, register, verify],
            interceptors=[CachingInterceptor("gs://bucket/cache")],
        )
    """

    def __init__(
        self,
        store_url: str,
        ttl: timedelta | None = None,
        key_fn: Callable[..., dict[str, Any]] | None = None,
        **storage_options: object,
    ) -> None:
        self._store = CacheStore(store_url, **storage_options)
        self._ttl = ttl
        self._key_fn = key_fn

    def intercept_activity(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
    ) -> temporalio.worker.ActivityInboundInterceptor:
        """Wrap the activity inbound interceptor with caching."""
        return _CachingActivityInboundInterceptor(next, self)


class _CachingActivityInboundInterceptor(
    temporalio.worker.ActivityInboundInterceptor,
):
    def __init__(
        self,
        next: temporalio.worker.ActivityInboundInterceptor,
        root: CachingInterceptor,
    ) -> None:
        super().__init__(next)
        self._root = root

    async def execute_activity(
        self,
        input: temporalio.worker.ExecuteActivityInput,
    ) -> Any:
        """Execute the activity with caching."""
        # Check opt-out marker
        if getattr(input.fn, NO_CACHE_ATTR, False):
            return await super().execute_activity(input)

        fn_name = getattr(input.fn, "__name__", str(input.fn))
        key = compute_cache_key(fn_name, input.args, self._root._key_fn)

        hit, value = await self._root._store.get(fn_name, key)
        if hit:
            logger.debug("Cache hit for %s (key=%s)", fn_name, key[:8])
            return value

        logger.debug("Cache miss for %s (key=%s)", fn_name, key[:8])
        result = await super().execute_activity(input)

        await self._root._store.set(fn_name, key, result, self._root._ttl)
        return result
