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

from typing import Any, Optional, Type

from pydantic import TypeAdapter
from pydantic_core import to_json

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


class PydanticJSONPlainPayloadConverter(EncodingPayloadConverter):
    """Pydantic JSON payload converter.

    Supports conversion of all types supported by Pydantic to and from JSON.

    In addition to Pydantic models, these include all `json.dump`-able types,
    various non-`json.dump`-able standard library types such as dataclasses,
    types from the datetime module, sets, UUID, etc, and custom types composed
    of any of these.

    See https://docs.pydantic.dev/latest/api/standard_library_types/
    """

    @property
    def encoding(self) -> str:
        """See base class."""
        return "json/plain"

    def to_payload(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """See base class.

        Uses ``pydantic_core.to_json`` to serialize ``value`` to JSON.

        See
        https://docs.pydantic.dev/latest/api/pydantic_core/#pydantic_core.to_json.
        """
        return temporalio.api.common.v1.Payload(
            metadata={"encoding": self.encoding.encode()}, data=to_json(value)
        )

    def from_payload(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
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

    def __init__(self) -> None:
        """Initialize object"""
        json_payload_converter = PydanticJSONPlainPayloadConverter()
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
