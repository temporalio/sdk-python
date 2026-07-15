"""Task result cache for continue-as-new support.

Caches task results by (module.qualname, args, kwargs) hash so that previously
completed tasks are not re-executed after a continue-as-new. The cache state
is a plain dict that can travel through workflow.continue_as_new().

The mechanism lives in ``temporalio.contrib._langchain._task_cache``; this
module binds the langgraph plugin's own cache instance and preserves the
original function surface.
"""

from __future__ import annotations

from typing import Any

from temporalio.contrib._langchain._task_cache import TaskResultCache
from temporalio.contrib._langchain._task_cache import cache_key as cache_key
from temporalio.contrib._langchain._task_cache import task_id as task_id

_cache = TaskResultCache("_temporal_task_cache")


def set_task_cache(cache: dict[str, Any] | None) -> None:
    """Set the task result cache for the current context."""
    _cache.set_cache(cache)


def get_task_cache() -> dict[str, Any] | None:
    """Get the task result cache for the current context."""
    return _cache.get_cache()


def cache_lookup(key: str) -> tuple[bool, Any]:
    """Return (True, value) if cached, (False, None) otherwise."""
    return _cache.lookup(key)


def cache_put(key: str, value: Any) -> None:
    """Store a value in the task result cache."""
    _cache.put(key, value)
