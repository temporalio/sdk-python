"""Cache key computation for activity memoization."""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, Callable

from temporalio.contrib.activity_cache._serializers import serialize_for_hash


def compute_cache_key(
    fn_name: str,
    args: tuple[Any, ...],
    key_fn: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Compute a deterministic cache key from a function name and arguments.

    Args:
        fn_name: The function/activity name.
        args: Positional arguments passed to the function.
        key_fn: Optional function that receives the activity arguments and
            returns a dict of values to include in the cache key. If provided,
            only the returned dict is hashed (not the full args).

    Returns:
        A 32-character hex string (SHA256 prefix).
    """
    if key_fn is not None:
        # Resolve args to kwargs for key_fn
        key_data = key_fn(*args)
        serialized = serialize_for_hash(key_data)
    else:
        serialized = serialize_for_hash(args)

    payload = json.dumps({"fn": fn_name, "args": serialized}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]
