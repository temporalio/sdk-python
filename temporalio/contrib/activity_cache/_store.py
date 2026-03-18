"""Cache store backed by fsspec for remote/local storage."""

from __future__ import annotations

import json
import pickle
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import fsspec


class CacheStore:
    """A key-value cache store backed by any fsspec-compatible filesystem.

    Cache entries are stored as two files per key:

    - ``{prefix}/{fn_name}/{key}.pkl`` — pickled return value
    - ``{prefix}/{fn_name}/{key}.meta.json`` — metadata (expiration)

    Args:
        base_url: Base URL for the cache store. The scheme determines the
            fsspec backend (``gs://`` for GCS, ``s3://`` for S3, etc.).
        **storage_options: Extra keyword arguments passed to
            ``fsspec.filesystem()``.
    """

    def __init__(self, base_url: str, **storage_options: object) -> None:
        self._base_url = base_url.rstrip("/")
        parsed = urlparse(self._base_url)
        self._protocol = parsed.scheme or "file"
        self._base_path = (
            parsed.netloc + parsed.path if parsed.netloc else parsed.path
        )
        self._fs = fsspec.filesystem(self._protocol, **storage_options)

    def _value_path(self, fn_name: str, key: str) -> str:
        return f"{self._base_path}/{fn_name}/{key}.pkl"

    def _meta_path(self, fn_name: str, key: str) -> str:
        return f"{self._base_path}/{fn_name}/{key}.meta.json"

    async def get(self, fn_name: str, key: str) -> tuple[bool, Any]:
        """Retrieve a cached value.

        Args:
            fn_name: The function/activity name (used as namespace).
            key: The cache key.

        Returns:
            A tuple of ``(hit, value)``. If ``hit`` is False, ``value``
            is None.
        """
        meta_path = self._meta_path(fn_name, key)

        if not self._fs.exists(meta_path):
            return False, None

        # Check TTL
        meta = json.loads(self._fs.cat_file(meta_path))
        expires_at = meta.get("expires_at")
        if expires_at is not None:
            if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
                # Expired — clean up lazily
                self._delete_entry(fn_name, key)
                return False, None

        value_path = self._value_path(fn_name, key)
        if not self._fs.exists(value_path):
            return False, None

        data = self._fs.cat_file(value_path)
        return True, pickle.loads(data)  # noqa: S301

    async def set(
        self, fn_name: str, key: str, value: Any, ttl: timedelta | None = None
    ) -> None:
        """Store a value in the cache.

        Args:
            fn_name: The function/activity name (used as namespace).
            key: The cache key.
            value: The value to cache (must be picklable).
            ttl: Optional time-to-live. If None, the entry never expires.
        """
        meta: dict[str, Any] = {}
        if ttl is not None:
            expires_at = datetime.now(timezone.utc) + ttl
            meta["expires_at"] = expires_at.isoformat()

        value_path = self._value_path(fn_name, key)
        meta_path = self._meta_path(fn_name, key)

        # Write value first, then metadata (metadata signals "entry exists")
        self._fs.pipe_file(value_path, pickle.dumps(value))
        self._fs.pipe_file(meta_path, json.dumps(meta).encode())

    async def delete(self, fn_name: str, key: str) -> None:
        """Delete a cache entry.

        Args:
            fn_name: The function/activity name (used as namespace).
            key: The cache key.
        """
        self._delete_entry(fn_name, key)

    def _delete_entry(self, fn_name: str, key: str) -> None:
        for path in [self._value_path(fn_name, key), self._meta_path(fn_name, key)]:
            if self._fs.exists(path):
                self._fs.rm(path)
