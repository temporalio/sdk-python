"""Continue-as-new result caching shared by the LangChain-family plugins."""

from __future__ import annotations

from contextvars import ContextVar
from hashlib import sha256
from json import dumps
from typing import Any


class TaskResultCache:
    """A workflow-scoped result cache carried across continue-as-new.

    Each plugin owns its own instance, so caches never share state across
    plugins. The backing store is a ``ContextVar``, so update / signal /
    query handler tasks spawned by the workflow inherit the same cache
    automatically. The cache itself is a plain dict that can travel through
    ``workflow.continue_as_new()``.

    ``set_cache`` stores the mapping AS GIVEN (identity semantics — the
    langgraph plugin exposes the live dict so mutations ride into the next
    run); callers that want copy-on-set semantics wrap at their own layer.
    """

    def __init__(self, context_var_name: str) -> None:
        """Create the cache with a uniquely named backing ``ContextVar``."""
        self._var: ContextVar[dict[str, Any] | None] = ContextVar(
            context_var_name, default=None
        )

    def set_cache(self, cache: dict[str, Any] | None) -> None:
        """Set the result cache for the current context (stored as given)."""
        self._var.set(cache)

    def get_cache(self) -> dict[str, Any] | None:
        """Get the result cache for the current context."""
        return self._var.get()

    def lookup(self, key: str) -> tuple[bool, Any]:
        """Return ``(True, value)`` if cached, ``(False, None)`` otherwise."""
        cache = self._var.get()
        if cache is not None and key in cache:
            return True, cache[key]
        return False, None

    def put(self, key: str, value: Any) -> None:
        """Store a value when a cache is active for this context."""
        cache = self._var.get()
        if cache is not None:
            cache[key] = value


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
