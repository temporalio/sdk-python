"""Task result cache for continue-as-new support.

Caches task results by (module.qualname, args, kwargs) hash so that previously
completed tasks are not re-executed after a continue-as-new. The cache state
is a plain dict that can travel through workflow.continue_as_new().
"""

from __future__ import annotations

from contextvars import ContextVar
from hashlib import sha256
from json import dumps
from typing import Any

_task_cache: ContextVar[dict[str, Any] | None] = ContextVar(
    "_temporal_task_cache", default=None
)


def set_task_cache(cache: dict[str, Any] | None) -> None:
    """Set the task result cache for the current context."""
    _task_cache.set(cache)


def get_task_cache() -> dict[str, Any] | None:
    """Get the task result cache for the current context."""
    return _task_cache.get()


def task_id(func: Any) -> str:
    """Return the fully-qualified module.qualname for a function.

    Raises ValueError for functions that cannot be identified unambiguously
    (lambdas, closures, __main__ functions).
    """
    module = getattr(func, "__module__", None)
    qualname = getattr(func, "__qualname__", None) or getattr(func, "__name__", None)

    if module is None or qualname is None:
        raise ValueError(
            f"Cannot identify task {func}: missing __module__ or __qualname__. "
            "Tasks must be defined at module level."
        )
    if module == "__main__":
        raise ValueError(
            f"Cannot identify task {qualname}: defined in __main__. "
            "Tasks must be importable from a named module."
        )
    if "<locals>" in qualname:
        raise ValueError(
            f"Cannot identify task {qualname}: closures/local functions are not supported. "
            "Tasks must be defined at module level."
        )
    return f"{module}.{qualname}"


def cache_key(
    task_id: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    context: Any = None,
) -> str:
    """Build a cache key from the full task identifier, arguments, and runtime context."""
    try:
        key_str = dumps([task_id, args, kwargs, context], sort_keys=True, default=str)
    except (TypeError, ValueError):
        key_str = repr([task_id, args, kwargs, context])
    return sha256(key_str.encode()).hexdigest()[:32]


def cache_lookup(key: str) -> tuple[bool, Any]:
    """Return (True, value) if cached, (False, None) otherwise."""
    cache = _task_cache.get()
    if cache is not None and key in cache:
        return True, cache[key]
    return False, None


def cache_put(key: str, value: Any) -> None:
    """Store a value in the task result cache."""
    cache = _task_cache.get()
    if cache is not None:
        cache[key] = value
