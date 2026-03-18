"""Input serialization for deterministic cache key computation."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import Any, Callable


# Registry of custom serializers: type → serializer function
_custom_serializers: dict[type, Callable[[Any], Any]] = {}


def register_serializer(type_: type, serializer: Callable[[Any], Any]) -> None:
    """Register a custom serializer for a type.

    The serializer function should return a JSON-serializable value that
    deterministically represents the input for cache key computation.

    Args:
        type_: The type to register the serializer for.
        serializer: A function that takes an instance of ``type_`` and returns
            a JSON-serializable representation.
    """
    _custom_serializers[type_] = serializer


def serialize_for_hash(value: Any) -> Any:
    """Convert a value to a JSON-serializable form for cache key computation.

    Handles common types deterministically:

    - Primitives (str, int, float, bool, None) pass through
    - bytes → SHA256 hash (first 16 chars)
    - Path → SHA256 of file content (first 16 chars)
    - Pydantic BaseModel → ``.model_dump()`` (recursive)
    - dataclass → ``dataclasses.asdict()`` (recursive)
    - dict → sorted keys, recursive values
    - list/tuple → recursive elements
    - Custom registered types → custom serializer

    Args:
        value: The value to serialize.

    Returns:
        A JSON-serializable representation suitable for hashing.

    Raises:
        TypeError: If the value type is not supported and no custom
            serializer is registered.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    # Check custom serializers first (allows overriding built-in behavior)
    for type_, serializer in _custom_serializers.items():
        if isinstance(value, type_):
            return serialize_for_hash(serializer(value))

    if isinstance(value, bytes):
        return {"__bytes__": hashlib.sha256(value).hexdigest()[:16]}

    if isinstance(value, Path):
        if value.is_file():
            content = value.read_bytes()
            return {"__path__": hashlib.sha256(content).hexdigest()[:16]}
        return {"__path__": str(value)}

    if isinstance(value, dict):
        return {str(k): serialize_for_hash(v) for k, v in sorted(value.items())}

    if isinstance(value, (list, tuple)):
        return [serialize_for_hash(item) for item in value]

    # Pydantic BaseModel (check without importing pydantic)
    if hasattr(value, "model_dump"):
        return serialize_for_hash(value.model_dump())

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return serialize_for_hash(dataclasses.asdict(value))

    raise TypeError(
        f"Cannot serialize type {type(value).__name__} for cache key. "
        f"Register a custom serializer with register_serializer()."
    )
