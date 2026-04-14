"""A data converter for Pydantic v2.

To use, pass ``pydantic_data_converter`` as the ``data_converter`` argument to
:py:class:`temporalio.client.Client`:

.. code-block:: python

    client = Client(
        data_converter=pydantic_data_converter,
        ...
    )

Pydantic v1 is not supported.
"""

import dataclasses
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter
from pydantic_core import SchemaSerializer, to_json
from pydantic_core.core_schema import any_schema

import temporalio.api.common.v1
from temporalio.converter import (
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    EncodingPayloadConverter,
    JSONPlainPayloadConverter,
)

# Note that in addition to the implementation in this module, _RestrictedProxy
# implements __get_pydantic_core_schema__ so that pydantic unwraps proxied types.


def _sanitize_for_json(obj: Any) -> Any:
    """Sanitize a value tree so pydantic_core's Rust serializer can encode it.

    Handles two cases that crash ``pydantic_core.to_json``:
    * **str** with Unicode surrogates (U+D800-U+DFFF)
    * **bytes** with non-UTF-8 content
    """
    if isinstance(obj, str):
        return obj.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace").encode("utf-8")
    if isinstance(obj, dict):
        new_dict: dict[Any, Any] = {}
        changed = False
        for k, v in obj.items():
            new_k = _sanitize_for_json(k)
            new_v = _sanitize_for_json(v)
            new_dict[new_k] = new_v
            if new_k is not k or new_v is not v:
                changed = True
        return new_dict if changed else obj
    if isinstance(obj, (list, tuple)):
        new_items = [_sanitize_for_json(item) for item in obj]
        changed = any(new is not old for new, old in zip(new_items, obj))
        if not changed:
            return obj
        return type(obj)(new_items)
    if hasattr(obj, "model_fields"):
        updates: dict[str, Any] = {}
        for field_name in obj.model_fields:
            val = getattr(obj, field_name)
            sanitized = _sanitize_for_json(val)
            if sanitized is not val:
                updates[field_name] = sanitized
        return obj.model_copy(update=updates) if updates else obj
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        updates = {}
        for f in dataclasses.fields(obj):
            val = getattr(obj, f.name)
            sanitized = _sanitize_for_json(val)
            if sanitized is not val:
                updates[f.name] = sanitized
        return dataclasses.replace(obj, **updates) if updates else obj
    return obj


@dataclass
class ToJsonOptions:
    """Options for converting to JSON with pydantic."""

    exclude_unset: bool = False
    lossy_utf8: bool = False
    """If ``True``, sanitize values that would crash pydantic_core's Rust
    serializer (strings with Unicode surrogates, bytes with non-UTF-8 content)
    instead of raising. Surrogates are replaced with U+FFFD and non-UTF-8 bytes
    are decoded with ``errors='replace'``. This is lossy but prevents
    serialization failures when payloads contain arbitrary binary data."""


class PydanticJSONPlainPayloadConverter(EncodingPayloadConverter):
    """Pydantic JSON payload converter.

    Supports conversion of all types supported by Pydantic to and from JSON.

    In addition to Pydantic models, these include all `json.dump`-able types,
    various non-`json.dump`-able standard library types such as dataclasses,
    types from the datetime module, sets, UUID, etc, and custom types composed
    of any of these.

    See https://docs.pydantic.dev/latest/api/standard_library_types/
    """

    def __init__(self, to_json_options: ToJsonOptions | None = None):
        """Create a new payload converter."""
        self._schema_serializer = SchemaSerializer(any_schema())
        self._to_json_options = to_json_options

    @property
    def encoding(self) -> str:
        """See base class."""
        return "json/plain"

    def to_payload(self, value: Any) -> temporalio.api.common.v1.Payload | None:
        """See base class.

        Uses ``pydantic_core.to_json`` to serialize ``value`` to JSON.

        See
        https://docs.pydantic.dev/latest/api/pydantic_core/#pydantic_core.to_json.
        """
        try:
            data = (
                self._schema_serializer.to_json(
                    value, exclude_unset=self._to_json_options.exclude_unset
                )
                if self._to_json_options
                else to_json(value)
            )
        except Exception:
            if not (self._to_json_options and self._to_json_options.lossy_utf8):
                raise
            # pydantic_core's Rust serializer cannot encode strings with
            # Unicode surrogates or bytes with non-UTF-8 content.
            # Sanitize the value tree, then retry the same serializer path.
            sanitized = _sanitize_for_json(value)
            data = (
                self._schema_serializer.to_json(
                    sanitized,
                    exclude_unset=self._to_json_options.exclude_unset,
                )
                if self._to_json_options
                else to_json(sanitized)
            )
        return temporalio.api.common.v1.Payload(
            metadata={"encoding": self.encoding.encode()}, data=data
        )

    def from_payload(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: type | None = None,
    ) -> Any:
        """See base class.

        Uses ``pydantic.TypeAdapter.validate_json`` to construct an
        instance of the type specified by ``type_hint`` from the JSON payload.

        See
        https://docs.pydantic.dev/latest/api/type_adapter/#pydantic.type_adapter.TypeAdapter.validate_json.
        """
        _type_hint = type_hint if type_hint is not None else Any
        return TypeAdapter(_type_hint).validate_json(payload.data)


class PydanticPayloadConverter(CompositePayloadConverter):
    """Payload converter for payloads containing pydantic model instances.

    JSON conversion is replaced with a converter that uses
    :py:class:`PydanticJSONPlainPayloadConverter`.
    """

    def __init__(self, to_json_options: ToJsonOptions | None = None) -> None:
        """Initialize object"""
        json_payload_converter = PydanticJSONPlainPayloadConverter(to_json_options)
        super().__init__(
            *(
                c
                if not isinstance(c, JSONPlainPayloadConverter)
                else json_payload_converter
                for c in DefaultPayloadConverter.default_encoding_payload_converters
            )
        )


pydantic_data_converter = DataConverter(
    payload_converter_class=PydanticPayloadConverter
)
"""Pydantic data converter.

Supports conversion of all types supported by Pydantic to and from JSON.

In addition to Pydantic models, these include all `json.dump`-able types,
various non-`json.dump`-able standard library types such as dataclasses,
types from the datetime module, sets, UUID, etc, and custom types composed
of any of these.

To use, pass as the ``data_converter`` argument of :py:class:`temporalio.client.Client`
"""
