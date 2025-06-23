"""DataConverter that supports conversion of types used by OpenAI Agents SDK.

These are mostly Pydantic types. Some of them should be explicitly imported.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Optional, Type, TypeVar

from agents import Usage
from agents.items import TResponseOutputItem
from openai import NOT_GIVEN, BaseModel
from pydantic import RootModel, TypeAdapter

import temporalio.api.common.v1
from temporalio import workflow
from temporalio.converter import (
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    EncodingPayloadConverter,
    JSONPlainPayloadConverter,
)

T = TypeVar("T", bound=BaseModel)


class _WrapperModel(RootModel[T]):
    model_config = {
        "arbitrary_types_allowed": True,
    }


class _OpenAIJSONPlainPayloadConverter(EncodingPayloadConverter):
    """Payload converter for OpenAI agent types that supports Pydantic models and standard Python types.

    This converter extends the standard JSON payload converter to handle OpenAI agent-specific
    types, particularly Pydantic models. It supports:

    1. All Pydantic models and their nested structures
    2. Standard JSON-serializable types
    3. Python standard library types like:
       - dataclasses
       - datetime objects
       - sets
       - UUIDs
    4. Custom types composed of any of the above

    The converter uses Pydantic's serialization capabilities to ensure proper handling
    of complex types while maintaining compatibility with Temporal's payload system.

    See https://docs.pydantic.dev/latest/api/standard_library_types/ for details
    on supported types.
    """

    @property
    def encoding(self) -> str:
        """Get the encoding identifier for this converter.

        Returns:
            The string "json/plain" indicating this is a plain JSON converter.
        """
        return "json/plain"

    def to_payload(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        """Convert a value to a Temporal payload.

        This method wraps the value in a Pydantic RootModel to handle arbitrary types
        and serializes it to JSON.

        Args:
            value: The value to convert to a payload.

        Returns:
            A Temporal payload containing the serialized value, or None if the value
            cannot be converted.
        """
        wrapper = _WrapperModel[Any](root=value)
        data = wrapper.model_dump_json(exclude_unset=True).encode()

        return temporalio.api.common.v1.Payload(
            metadata={"encoding": self.encoding.encode()}, data=data
        )

    def from_payload(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: Optional[Type] = None,
    ) -> Any:
        """Convert a Temporal payload back to a Python value.

        This method deserializes the JSON payload and validates it against the
        provided type hint using Pydantic's validation system.

        Args:
            payload: The Temporal payload to convert.
            type_hint: Optional type hint for validation.

        Returns:
            The deserialized and validated value.

        Note:
            The type hint is used for validation but the actual type returned
            may be a Pydantic model instance.
        """
        _type_hint = type_hint or Any
        wrapper = _WrapperModel[_type_hint]  # type: ignore[valid-type]

        with workflow.unsafe.imports_passed_through():
            with workflow.unsafe.sandbox_unrestricted():
                wrapper.model_rebuild(
                    _types_namespace=_get_openai_modules()
                    | {"TResponseOutputItem": TResponseOutputItem, "Usage": Usage}
                )
        return TypeAdapter(wrapper).validate_json(payload.data.decode()).root


def _get_openai_modules() -> dict[Any, Any]:
    def get_modules(module):
        result_dict: dict[Any, Any] = {}
        for _, mod in inspect.getmembers(module, inspect.ismodule):
            result_dict |= mod.__dict__ | get_modules(mod)
        return result_dict

    return get_modules(importlib.import_module("openai.types"))


class OpenAIPayloadConverter(CompositePayloadConverter):
    """Payload converter for payloads containing pydantic model instances.

    JSON conversion is replaced with a converter that uses
    :py:class:`PydanticJSONPlainPayloadConverter`.
    """

    def __init__(self) -> None:
        """Initialize object"""
        json_payload_converter = _OpenAIJSONPlainPayloadConverter()
        super().__init__(
            *(
                c
                if not isinstance(c, JSONPlainPayloadConverter)
                else json_payload_converter
                for c in DefaultPayloadConverter.default_encoding_payload_converters
            )
        )


open_ai_data_converter = DataConverter(payload_converter_class=OpenAIPayloadConverter)
"""Open AI Agent library types data converter"""
