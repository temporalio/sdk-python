"""Deterministic span ID generation for Temporal Datadog tracing."""

_FNV_OFFSET_64 = 0xCBF29CE484222325
_FNV_PRIME_64 = 0x100000001B3
_FNV_MASK_64 = 0xFFFFFFFFFFFFFFFF


def gen_trace_id(key: str) -> int:
    """Compute a deterministic 64-bit trace ID for a root RunWorkflow span.

    Used when no Datadog trace header is present (uninstrumented client), so
    that the trace ID is stable across worker restarts for the same run.

    Uses the same FNV-1 algorithm as gen_span_id but prefixes the input with
    ``trace:`` to keep trace-ID inputs distinct from span-ID inputs.
    """
    return gen_span_id(f"trace:{key}")


def gen_span_id(key: str) -> int:
    """Compute a 64-bit FNV-1 hash of a UTF-8 encoded string.

    Used to derive deterministic Datadog span IDs from the composite
    idempotency keys built by the tracing interceptors.

    Matches the byte-for-byte output of Go's ``hash/fnv.New64()`` (which is
    FNV-1, not FNV-1a), so Go and Python workers hashing the same
    idempotency key produce the same span ID.

    Args:
        key: The string to hash.

    Returns:
        A 64-bit unsigned integer suitable for use as a Datadog span ID.
    """
    h = _FNV_OFFSET_64
    for byte in key.encode("utf-8"):
        h = (h * _FNV_PRIME_64) & _FNV_MASK_64
        h ^= byte
    return h
