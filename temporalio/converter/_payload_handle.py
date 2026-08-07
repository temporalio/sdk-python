"""Payload handles: lazy, pass-by-reference payload values.

A :py:class:`PayloadHandle` is used as a parameter or return annotation
(``PayloadHandle[T]``) to defer *acquiring* a value -- external-storage
retrieval, codec decoding, and deserialization. A handle is a plain, immutable
*value*: it carries the opaque, end-of-pipeline payload and the inner type, and
can be forwarded (e.g. from a workflow to an activity) without paying to
acquire it.

The handle deliberately owns no behavior beyond introspection of what it
carries. Acquiring the value needs machinery -- a data converter, codec chain,
and storage driver -- that belongs to an execution surface (the activity worker
or the client), not to a payload value. So acquisition is a boundary operation
(:py:meth:`temporalio.converter.DataConverter.get_handle_value`, and
:py:func:`temporalio.activity.get_handle_value` in activity code), never a
method on the handle. This keeps the handle portable with no captured runtime
state, and avoids relying on an ambient mechanism to inject a converter.

This mirrors :py:class:`temporalio.common.RawValue`: the annotation, not any
wire encoding, is what triggers handle behavior, so a handle works on any
already-stored payload in history and is replay-safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Generic,
    Optional,
    get_args,
    get_origin,
)

import temporalio.api.common.v1
from temporalio.types import AnyType


@dataclass(frozen=True)
class PayloadHandle(Generic[AnyType]):
    """A lazy, immutable, pass-by-reference handle to a payload value.

    Annotate a workflow/activity/signal parameter or return value as
    ``PayloadHandle[T]`` to receive one of these instead of the materialized
    value. Forward it onward without cost, and acquire its value at a boundary
    (an activity or client) where fetching data is permitted, via
    :py:func:`temporalio.activity.get_handle_value` or
    :py:meth:`temporalio.converter.DataConverter.get_handle_value`. A handle
    does not acquire its own value.
    """

    # The opaque end-of-pipeline payload (may be an external-storage reference
    # or a codec-encoded inline payload). The handle is backing-agnostic; this
    # is read by the boundary converter when acquiring the value.
    _payload: temporalio.api.common.v1.Payload
    # Inner type ``T`` captured from the annotation, used as the decode hint
    # when acquiring the value. May be None for a bare ``PayloadHandle``.
    _type: Optional[type] = field(default=None, compare=False)

    def __getstate__(self) -> object:
        """Pickle support (workflow sandbox caching)."""
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


class AsHandle:
    """Marker for ``Annotated[T, AsHandle]``: consume ``T`` as a forward-only
    :py:class:`PayloadHandle` without ``T`` leaving the shared contract.

    ``Annotated[T, AsHandle]`` is transparent to type checkers (both a caller and
    the callee still see ``T``), so a caller passes a plain ``T`` -- no coupling.
    The SDK recovers the marker via ``get_type_hints(..., include_extras=True)``
    and delivers a handle instead of materializing. This is the *forward-only*
    (type-erased) option: the callee's variable is still statically ``T``, so it
    can forward the value but cannot call handle methods on it statically.
    """


def _is_payload_handle_hint(hint: Any) -> bool:
    """Return True for ``PayloadHandle``, ``PayloadHandle[T]``, or ``Annotated[T, AsHandle]``."""
    return (
        hint is PayloadHandle
        or get_origin(hint) is PayloadHandle
        or AsHandle in getattr(hint, "__metadata__", ())
    )


def _payload_handle_inner_type(hint: Any) -> Optional[type]:
    """Return ``T`` from ``PayloadHandle[T]`` or ``Annotated[T, AsHandle]``, else None."""
    # Annotated[T, AsHandle]: the base type T is the inner (materialized) type.
    if AsHandle in getattr(hint, "__metadata__", ()):
        return hint.__origin__
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
    """Build a data-only handle (no captured converter)."""
    return PayloadHandle(payload, inner_type)
