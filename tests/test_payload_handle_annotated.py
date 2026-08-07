"""Demonstration of the ``Annotated[T, AsHandle]`` forward-only consumption option.

This is the "type-erased" option from the consumption-decoupling design: the
shared contract type stays ``T``, and a *marker* on the annotation (not the type
itself) drives handle consumption. A caller passes a plain ``T`` because
``Annotated[T, AsHandle]`` is transparent to type checkers, so there is no
coupling; the SDK recovers the marker and delivers a forward-only
:py:class:`PayloadHandle` instead of materializing.

Scope note: this exercises the converter layer directly, which is where the
mechanism lives. It does NOT yet work end to end through a running workflow,
because the SDK resolves argument/return types with ``get_type_hints()`` WITHOUT
``include_extras`` (``temporalio/common.py`` around line 1398), which strips the
``Annotated`` metadata before the converter ever sees it. Turning that on (and
threading it through arg-type resolution) is the remaining wiring; this test
isolates and demonstrates the underlying mechanism.
"""

from __future__ import annotations

from typing import Annotated, Any, get_type_hints

from temporalio.converter import DataConverter, PayloadHandle
from temporalio.converter._payload_handle import (
    AsHandle,
    _is_payload_handle_hint,
    _payload_handle_inner_type,
)


def test_annotated_is_transparent_but_marker_recoverable() -> None:
    def handler(data: Annotated[str, AsHandle]) -> None: ...

    # A caller and a type checker see a plain `str` -- the contract is unchanged,
    # so there is no coupling: the caller passes a `str`, not a PayloadHandle.
    assert get_type_hints(handler)["data"] is str
    # The SDK can still recover the marker when it asks for extras.
    assert (
        get_type_hints(handler, include_extras=True)["data"] == Annotated[str, AsHandle]
    )


def test_annotated_marker_is_recognized_as_a_handle_hint() -> None:
    hint = Annotated[str, AsHandle]
    assert _is_payload_handle_hint(hint)
    # The inner (materialized) type is the base type of the annotation.
    assert _payload_handle_inner_type(hint) is str


async def test_same_payload_consumed_as_value_or_handle_by_marker() -> None:
    dc = DataConverter()
    # An ordinary inline payload (no external storage): the mechanism is neutral
    # to the payload's shape -- it does not require an offloaded reference.
    [payload] = await dc.encode(["big-value"])

    # Consumed as the contract type -> the materialized value.
    [value] = await dc.decode([payload], [str])
    assert value == "big-value"

    # Consumed via the marker -> a PayloadHandle, with no change to the `str`
    # contract. Its value is acquired at the boundary through the converter.
    handle_hint: Any = Annotated[str, AsHandle]
    [handle] = await dc.decode([payload], [handle_hint])
    assert isinstance(handle, PayloadHandle)
    assert await dc.get_handle_value(handle) == "big-value"


async def test_annotated_handle_is_a_data_only_value() -> None:
    dc = DataConverter()
    [payload] = dc.payload_converter.to_payloads(["big-value"])

    # Sync conversion, as inside the workflow sandbox, yields a data-only handle:
    # it carries the payload but owns no acquire behavior. Forward-only-ness is a
    # property of the workflow surface (which does not expose get_handle_value),
    # not of the handle; acquisition is a boundary operation.
    handle_hint: Any = Annotated[str, AsHandle]
    [handle] = dc.payload_converter.from_payloads([payload], [handle_hint])
    assert isinstance(handle, PayloadHandle)
    assert not hasattr(handle, "materialize")
    # Through a boundary converter the value is acquirable.
    assert await dc.get_handle_value(handle) == "big-value"
