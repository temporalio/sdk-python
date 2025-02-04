"""A data converter for Pydantic models

To use, pass ``pydantic_data_converter`` as the ``data_converter`` argument to
:py:class:`temporalio.client.Client`:

.. code-block:: python

    client = Client(
        data_converter=pydantic_data_converter,
        ...
    )
"""

import inspect
from typing import (
    Any,
    Type,
)

import pydantic

try:
    from pydantic_core import to_jsonable_python
except ImportError:
    # pydantic v1
    from pydantic.json import pydantic_encoder as to_jsonable_python  # type: ignore

import temporalio.workflow
from temporalio.converter import (
    AdvancedJSONEncoder,
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    JSONPlainPayloadConverter,
    JSONTypeConverter,
)
from temporalio.worker.workflow_sandbox._restrictions import RestrictionContext

# Note that in addition to the implementation in this module, _RestrictedProxy
# implements __get_pydantic_core_schema__ so that pydantic unwraps proxied types
# when determining the schema.


class PydanticModelTypeConverter(JSONTypeConverter):
    """Type converter for pydantic model instances."""

    def to_typed_value(self, hint: Type, value: Any) -> Any:
        """Convert dict value to pydantic model instance of the specified type"""
        if not inspect.isclass(hint) or not issubclass(hint, pydantic.BaseModel):
            return JSONTypeConverter.Unhandled
        model = hint
        if not isinstance(value, dict):
            raise TypeError(
                f"Cannot convert to {model}, value is {type(value)} not dict"
            )
        if temporalio.workflow.unsafe.in_sandbox():
            # Unwrap proxied model field types so that Pydantic can call their constructors
            model = pydantic.create_model(
                model.__name__,
                **{  # type: ignore
                    name: (RestrictionContext.unwrap_if_proxied(f.annotation), f)
                    for name, f in model.model_fields.items()
                },
            )
        if hasattr(model, "model_validate"):
            return model.model_validate(value)
        elif hasattr(model, "parse_obj"):
            # pydantic v1
            return model.parse_obj(value)
        else:
            raise ValueError(
                f"{model} is a Pydantic model but does not have a `model_validate` or `parse_obj` method"
            )


class PydanticJSONEncoder(AdvancedJSONEncoder):
    """JSON encoder for python objects containing pydantic model instances."""

    def default(self, o: Any) -> Any:
        """Convert object to jsonable python.

        See :py:meth:`json.JSONEncoder.default`.
        """
        if isinstance(o, pydantic.BaseModel):
            return to_jsonable_python(o)
        return super().default(o)


class PydanticPayloadConverter(CompositePayloadConverter):
    """Payload converter for payloads containing pydantic model instances.

    JSON conversion is replaced with a converter that uses
    :py:class:`PydanticJSONEncoder` to convert the python object to JSON, and
    :py:class:`PydanticModelTypeConverter` to convert raw python values to
    pydantic model instances.
    """

    def __init__(self) -> None:
        """Initialize object"""
        json_payload_converter = JSONPlainPayloadConverter(
            encoder=PydanticJSONEncoder,
            custom_type_converters=[PydanticModelTypeConverter()],
        )
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
"""Data converter for payloads containing pydantic model instances.

To use, pass as the ``data_converter`` argument of :py:class:`temporalio.client.Client`
"""
