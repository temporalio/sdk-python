"""Cached decorator for activity memoization."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any, TypeVar

from temporalio.contrib.activity_cache._keys import compute_cache_key
from temporalio.contrib.activity_cache._store import CacheStore

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Marker attribute to opt out of caching via interceptor
NO_CACHE_ATTR = "__temporal_no_cache__"


def no_cache(fn: F) -> F:
    """Mark an activity to be excluded from interceptor-based caching.

    Use this when using :class:`CachingInterceptor` but you want specific
    activities to always execute without caching.

    Example::

        @no_cache
        @activity.defn
        async def always_runs(input: Input) -> Output:
            ...
    """
    setattr(fn, NO_CACHE_ATTR, True)
    return fn


def cached(
    store_url: str,
    ttl: timedelta | None = None,
    key_fn: Callable[..., dict[str, Any]] | None = None,
    **storage_options: object,
) -> Callable[[F], F]:
    """Decorator for content-addressed activity memoization.

    Wraps an async function so that repeated calls with the same inputs
    return the cached result without re-executing. The cache is stored
    remotely (GCS, S3, local, etc.) so it is shared across distributed
    workers.

    Args:
        store_url: Base URL for the cache store. The scheme determines the
            backend (``gs://`` for GCS, ``s3://`` for S3, etc.).
        ttl: Optional time-to-live for cached entries. If None, entries
            never expire.
        key_fn: Optional function that receives the activity arguments and
            returns a dict of values to hash for the cache key. If not
            provided, all arguments are included in the key.
        **storage_options: Extra keyword arguments passed to the fsspec
            filesystem (e.g., ``project="my-gcp-project"``).

    Example::

        @cached("gs://my-bucket/cache", ttl=timedelta(days=90))
        @activity.defn
        async def extract(input: ExtractInput) -> ExtractOutput:
            ...  # Only runs on cache miss

        # With key_fn to select which args matter:
        @cached(
            "gs://my-bucket/cache",
            key_fn=lambda input: {
                "component": input.component,
                "content_hash": input.content_hash,
            },
        )
        @activity.defn
        async def extract(input: ExtractInput) -> ExtractOutput:
            ...
    """
    store = CacheStore(store_url, **storage_options)

    def decorator(fn: F) -> F:
        fn_name = getattr(fn, "__name__", str(fn))

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = compute_cache_key(fn_name, args, key_fn)

            hit, value = await store.get(fn_name, key)
            if hit:
                logger.debug("Cache hit for %s (key=%s)", fn_name, key[:8])
                return value

            logger.debug("Cache miss for %s (key=%s)", fn_name, key[:8])
            result = await fn(*args, **kwargs)

            await store.set(fn_name, key, result, ttl)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
