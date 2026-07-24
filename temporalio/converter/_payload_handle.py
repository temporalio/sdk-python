"""Payload handles: lazy, pass-by-reference payload values.

A :py:class:`PayloadHandle` is used as a parameter or return annotation
(``PayloadHandle[T]``) to defer *acquiring* a value -- external-storage
retrieval, codec decoding, and deserialization -- until it is explicitly
awaited via :py:meth:`PayloadHandle.materialize`. Until then the handle just
carries the opaque, end-of-pipeline payload and can be forwarded (e.g. from a
workflow to an activity) without paying to materialize it.

This mirrors :py:class:`temporalio.common.RawValue`: the annotation, not any
wire encoding, is what triggers handle behavior, so a handle works on any
already-stored payload in history and is replay-safe.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Iterator,
    Optional,
    cast,
    get_args,
    get_origin,
)

import temporalio.api.common.v1
from temporalio.types import AnyType

if TYPE_CHECKING:
    from temporalio.converter._data_converter import DataConverter


# The data converter needed to materialize a handle only exists at the async
# worker/client boundary (never inside the workflow sandbox, where I/O is
# forbidden). Boundary decodes publish it here so handles built during
# conversion can bind to it; the sandbox leaves it unset, yielding forward-only
# handles.
_current_data_converter: contextvars.ContextVar[Optional[DataConverter]] = (
    contextvars.ContextVar("_temporal_payload_handle_data_converter", default=None)
)


@contextmanager
def _bind_data_converter(data_converter: DataConverter) -> Iterator[None]:
    """Bind the data converter used by handles created within this context."""
    token = _current_data_converter.set(data_converter)
    try:
        yield
    finally:
        _current_data_converter.reset(token)


@dataclass(frozen=True)
class PayloadHandle(Generic[AnyType]):
    """A lazy, immutable, pass-by-reference handle to a payload value.

    Annotate a workflow/activity/signal parameter or return value as
    ``PayloadHandle[T]`` to receive one of these instead of the materialized
    value. Forward it onward without cost, or call
    :py:meth:`materialize` where the value is actually needed.
    """

    # The opaque end-of-pipeline payload (may be an external-storage reference
    # or a codec-encoded inline payload). Kept private: the handle is
    # backing-agnostic and exposes nothing about how the value is stored.
    _payload: temporalio.api.common.v1.Payload
    # Inner type ``T`` captured from the annotation, used as the decode hint at
    # materialize time. May be None for a bare ``PayloadHandle`` annotation.
    _type: Optional[type] = field(default=None, compare=False)
    # Set only for handles created at the async boundary; None => forward-only.
    _data_converter: Optional[DataConverter] = field(
        default=None, compare=False, repr=False
    )

    async def materialize(self) -> AnyType:
        """Acquire and return the underlying value.

        Runs the deferred inbound pipeline (external-storage retrieval if
        offloaded, codec decoding, then deserialization into the real type
        ``T`` captured from the ``PayloadHandle[T]`` annotation). The return type
        is that ``T`` -- annotate handles as ``PayloadHandle[T]`` so callers keep
        full type information rather than an untyped value.

        Raises:
            RuntimeError: if the handle is forward-only (e.g. received inside a
                workflow, where acquisition I/O is not permitted), or if it
                carries no real type (a bare ``PayloadHandle`` annotation), since
                payload conversion needs a concrete type.
        """
        data_converter = self._data_converter
        if data_converter is None:
            raise RuntimeError(
                "[TMPRL1106] PayloadHandle is forward-only in this context "
                "(such as inside a workflow) and cannot be materialized. Forward "
                "it to an activity, or materialize it from client code, instead."
            )
        if self._type is None:
            raise RuntimeError(
                "[TMPRL1106] PayloadHandle has no type to materialize into. "
                "Annotate the value as PayloadHandle[T] with a concrete type T."
            )
        # Reuse the standard inbound transform (retrieve -> codec-decode) that
        # eager decoding would have applied, then deserialize to the real type.
        payload = await data_converter._transform_inbound_payload(self._payload)
        [value] = data_converter.payload_converter.from_payloads(
            [payload], [self._type]
        )
        return cast(AnyType, value)

    def __getstate__(self) -> object:
        """Pickle support (workflow sandbox caching).

        Excludes the bound data converter so a rehydrated handle is forward-only,
        reinforcing that materialization never happens on the sandbox side.
        """
        return {"payload": self._payload.SerializeToString(), "type": self._type}

    def __setstate__(self, state: object) -> None:
        """Pickle support."""
        if not isinstance(state, dict):
            raise TypeError(f"Expected dict state, got {type(state)}")
        object.__setattr__(
            self,
            "_payload",
            temporalio.api.common.v1.Payload.FromString(state["payload"]),
        )
        object.__setattr__(self, "_type", state.get("type"))
        object.__setattr__(self, "_data_converter", None)


def _is_payload_handle_hint(hint: Any) -> bool:
    """Return True if a type hint is ``PayloadHandle`` or ``PayloadHandle[T]``."""
    return hint is PayloadHandle or get_origin(hint) is PayloadHandle


def _payload_handle_inner_type(hint: Any) -> Optional[type]:
    """Return ``T`` from ``PayloadHandle[T]``, or None for a bare hint."""
    args = get_args(hint)
    return args[0] if args else None


def _payload_handle_hint(inner_type: Optional[type]) -> Any:
    """Build a ``PayloadHandle[inner_type]`` hint (bare if ``inner_type`` is None).

    Used to upgrade a call's result type so an unchanged activity/child result
    is consumed as a handle: the declared return type becomes the handle's ``T``.
    """
    return (
        PayloadHandle[inner_type]  # type: ignore[valid-type]
        if inner_type is not None
        else PayloadHandle
    )


def _create_handle(
    payload: temporalio.api.common.v1.Payload, inner_type: Optional[type]
) -> PayloadHandle[Any]:
    """Build a handle, binding it to the current boundary converter if any."""
    return PayloadHandle(payload, inner_type, _current_data_converter.get())
