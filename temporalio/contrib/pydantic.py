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

import functools
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


@dataclass
class ToJsonOptions:
    """Options for converting to JSON with pydantic."""

    exclude_unset: bool = False


# `TypeAdapter.__init__` builds a core schema. Pydantic caches that on the class
# for `BaseModel` subclasses, but not for non-class hints such as `Annotated`
# discriminated unions, `list[Union[...]]` or generics, so constructing one per
# payload rebuilds the schema every time and construction dominates validation.
# The cache is bounded because hints are held strongly; a worker sees a small,
# fixed set of activity and workflow signatures, so a modest ceiling covers them
# while keeping a pathological caller from retaining unbounded types.
_TYPE_ADAPTER_CACHE_SIZE = 256


@functools.lru_cache(maxsize=_TYPE_ADAPTER_CACHE_SIZE)
def _cached_type_adapter(type_hint: Any) -> TypeAdapter[Any]:
    return TypeAdapter(type_hint)


def _type_adapter_for(type_hint: Any) -> TypeAdapter[Any]:
    """Return a ``TypeAdapter`` for ``type_hint``, reusing one where possible."""
    try:
        return _cached_type_adapter(type_hint)
    except TypeError:
        # Unhashable hints (or hints holding unhashable metadata) cannot be cache
        # keys. Fall back to constructing per call rather than failing conversion.
        return TypeAdapter(type_hint)


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
        data = (
            self._schema_serializer.to_json(
                value, exclude_unset=self._to_json_options.exclude_unset
            )
            if self._to_json_options
            else to_json(value)
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
        return _type_adapter_for(_type_hint).validate_json(payload.data)


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
