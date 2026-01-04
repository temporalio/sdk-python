"""In-memory cache for LangGraph Functional API task result caching.

This cache implements LangGraph's BaseCache interface and stores task results
in memory. It supports serialization via get_state() for continue-as-new
workflows, and can be initialized from a previously serialized state.

Usage:
    # Create new cache
    cache = InMemoryCache()

    # Or restore from continue-as-new state
    cache = InMemoryCache(state=checkpoint.get("cache_state"))

    # Before continue-as-new, serialize the cache
    checkpoint = {"cache_state": cache.get_state()}
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any, Generic, TypeVar

# Type definitions matching LangGraph's cache types
Namespace = tuple[str, ...]
"""A namespace is a tuple of strings identifying a cache scope."""

FullKey = tuple[Namespace, str]
"""A full cache key is a (namespace, key) tuple."""

ValueT = TypeVar("ValueT")

__all__ = ["InMemoryCache", "Namespace", "FullKey"]


class InMemoryCache(Generic[ValueT]):
    """In-memory cache that stores task results with optional TTL.

    This cache can be serialized for continue-as-new workflows and restored
    from a previously serialized state.

    Attributes:
        _cache: Internal storage mapping FullKey to (value, expiration_time).
    """

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        """Initialize the cache.

        Args:
            state: Optional serialized state from a previous cache instance
                   (from get_state()). If provided, restores the cache state.
        """
        # Internal storage: FullKey -> (value, expiration_timestamp or None)
        self._cache: dict[FullKey, tuple[ValueT, float | None]] = {}

        if state is not None:
            self._restore_from_state(state)

    def _restore_from_state(self, state: dict[str, Any]) -> None:
        """Restore cache from serialized state.

        Args:
            state: Serialized state from get_state().
        """
        entries = state.get("entries", [])
        for entry in entries:
            # Reconstruct FullKey from serialized form
            namespace = tuple(entry["namespace"])
            key = entry["key"]
            full_key: FullKey = (namespace, key)

            value = entry["value"]
            expires_at = entry.get("expires_at")

            # Only restore if not expired
            if expires_at is None or expires_at > time.time():
                self._cache[full_key] = (value, expires_at)

    def get_state(self) -> dict[str, Any]:
        """Serialize cache state for continue-as-new.

        Returns:
            Serializable dict containing cache entries.
        """
        entries = []
        current_time = time.time()

        for full_key, (value, expires_at) in self._cache.items():
            # Skip expired entries
            if expires_at is not None and expires_at <= current_time:
                continue

            namespace, key = full_key
            entries.append(
                {
                    "namespace": list(namespace),  # Convert tuple to list for JSON
                    "key": key,
                    "value": value,
                    "expires_at": expires_at,
                }
            )

        return {"entries": entries}

    def _is_expired(self, expires_at: float | None) -> bool:
        """Check if an entry is expired."""
        if expires_at is None:
            return False
        return time.time() > expires_at

    # Synchronous interface (required by BaseCache)

    def get(self, keys: Sequence[FullKey]) -> dict[FullKey, ValueT]:
        """Get cached values for the given keys.

        Args:
            keys: Sequence of FullKey tuples to look up.

        Returns:
            Dict mapping found keys to their cached values.
            Keys not found or expired are not included.
        """
        result: dict[FullKey, ValueT] = {}
        for key in keys:
            if key in self._cache:
                value, expires_at = self._cache[key]
                if not self._is_expired(expires_at):
                    result[key] = value
                else:
                    # Clean up expired entry
                    del self._cache[key]
        return result

    def set(self, pairs: Mapping[FullKey, tuple[ValueT, int | None]]) -> None:
        """Set cached values with optional TTL.

        Args:
            pairs: Mapping of FullKey to (value, ttl_seconds).
                   TTL of None means no expiration.
        """
        current_time = time.time()
        for key, (value, ttl) in pairs.items():
            expires_at = current_time + ttl if ttl is not None else None
            self._cache[key] = (value, expires_at)

    def clear(self, namespaces: Sequence[Namespace] | None = None) -> None:
        """Clear cached entries.

        Args:
            namespaces: If provided, only clear entries in these namespaces.
                       If None, clear all entries.
        """
        if namespaces is None:
            self._cache.clear()
        else:
            namespace_set = set(namespaces)
            keys_to_delete = [key for key in self._cache if key[0] in namespace_set]
            for key in keys_to_delete:
                del self._cache[key]

    # Async interface (required by BaseCache for async execution)

    async def aget(self, keys: Sequence[FullKey]) -> dict[FullKey, ValueT]:
        """Async version of get()."""
        return self.get(keys)

    async def aset(self, pairs: Mapping[FullKey, tuple[ValueT, int | None]]) -> None:
        """Async version of set()."""
        self.set(pairs)

    async def aclear(self, namespaces: Sequence[Namespace] | None = None) -> None:
        """Async version of clear()."""
        self.clear(namespaces)

    # Utility methods

    def __len__(self) -> int:
        """Return number of entries in cache (including potentially expired)."""
        return len(self._cache)

    def __contains__(self, key: FullKey) -> bool:
        """Check if key exists and is not expired."""
        if key not in self._cache:
            return False
        _, expires_at = self._cache[key]
        return not self._is_expired(expires_at)
