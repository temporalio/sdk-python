"""The shared LangChain-family payload converter composition."""

from __future__ import annotations

import dataclasses

import pytest

from temporalio.converter import DataConverter

pytest.importorskip("pydantic")

from temporalio.contrib._langchain._converter import (  # noqa: E402
    LangChainPayloadConverter,
    build_data_converter,
    data_converter,
)


def test_none_installs_family_default() -> None:
    assert build_data_converter(None, plugin_name="XPlugin") is data_converter


def test_sdk_default_gets_converter_swapped() -> None:
    result = build_data_converter(DataConverter.default, plugin_name="XPlugin")
    assert result.payload_converter_class is LangChainPayloadConverter
    # Everything else on the SDK default is preserved (dataclasses.replace
    # rebuilds derived instance attributes, so compare the declared fields).
    assert (
        result.failure_converter_class is DataConverter.default.failure_converter_class
    )
    assert result.payload_codec is DataConverter.default.payload_codec


def test_custom_converter_is_refused_with_plugin_name() -> None:
    custom = dataclasses.replace(DataConverter.default)
    with pytest.raises(ValueError, match="XPlugin cannot compose"):
        build_data_converter(custom, plugin_name="XPlugin")


def test_exclude_unset_round_trip() -> None:
    from pydantic import BaseModel

    class Inner(BaseModel):
        a: int = 1
        b: str | None = None

    class Outer(BaseModel):
        inner: Inner
        note: str | None = None

    converter = LangChainPayloadConverter()
    payload = converter.to_payload(Outer(inner=Inner(a=2)))
    assert payload is not None
    # Unset defaults are excluded from the wire form.
    assert b'"b"' not in payload.data
    assert b'"note"' not in payload.data
    restored = converter.from_payload(payload, Outer)
    assert restored.inner.a == 2 and restored.note is None
