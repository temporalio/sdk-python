"""Converter-internal helpers for value handles.

The user-facing :py:class:`temporalio.common.ValueHandle` type and the
``AsHandle`` marker live in :py:mod:`temporalio.common`, next to
:py:class:`temporalio.common.RawValue`. This module holds only the
converter-internal helpers that recognize handle type hints and build handles
during payload conversion.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional, get_args, get_origin

import temporalio.api.common.v1
from temporalio.common import (
    _HANDLE_METADATA_PREFIX,
    AsHandle,
    ValueHandle,
)


def _is_payload_handle_hint(hint: Any) -> bool:
    """Return True for ``ValueHandle``, ``ValueHandle[T]``, or ``Annotated[T, AsHandle]``."""
    return (
        hint is ValueHandle
        or get_origin(hint) is ValueHandle
        or AsHandle in getattr(hint, "__metadata__", ())
    )


def _payload_handle_inner_type(hint: Any) -> Optional[type]:
    """Return ``T`` from ``ValueHandle[T]`` or ``Annotated[T, AsHandle]``, else None."""
    # Annotated[T, AsHandle]: the base type T is the inner (materialized) type.
    if AsHandle in getattr(hint, "__metadata__", ()):
        return hint.__origin__
    args = get_args(hint)
    return args[0] if args else None


def _upgrade_result_hint(ret_type: Any) -> Any:
    """Upgrade a call's result type so its result is consumed as a handle.

    The declared return type becomes the handle's ``T``
    (``ValueHandle[ret_type]``, or bare ``ValueHandle`` if the type is unknown).
    Idempotent, so a callee that already returns a ``ValueHandle`` is not
    double-wrapped.
    """
    if _is_payload_handle_hint(ret_type):
        return ret_type
    return (
        ValueHandle[ret_type]  # type: ignore[valid-type]
        if ret_type is not None
        else ValueHandle
    )


def _create_handle(
    payload: temporalio.api.common.v1.Payload, inner_type: Optional[type]
) -> ValueHandle[Any]:
    """Build a data-only handle (no captured converter)."""
    return ValueHandle(payload, inner_type)


def _attach_metadata(
    payload: temporalio.api.common.v1.Payload, metadata: Mapping[str, str]
) -> None:
    """Attach user metadata to a payload as server-opaque, prefixed keys."""
    for key, value in metadata.items():
        payload.metadata[_HANDLE_METADATA_PREFIX + key] = value.encode()
