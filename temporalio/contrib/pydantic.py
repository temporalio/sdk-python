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
    from pydantic.json import pydantic_encoder as to_jsonable_python

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


class PydanticModelTypeConverter(JSONTypeConverter):
    def to_typed_value(self, hint: Type, value: Any) -> Any:
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
    def default(self, o: Any) -> Any:
        if isinstance(o, pydantic.BaseModel):
            return to_jsonable_python(o)
        return super().default(o)


class PydanticPayloadConverter(CompositePayloadConverter):
    """Pydantic payload converter.

    Payload converter that replaces the default JSON conversion with Pydantic
    JSON conversion.
    """

    def __init__(self) -> None:
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
"""Data converter for Pydantic models.

To use, pass this as the ``data_converter`` argument to :py:class:`temporalio.client.Client`
"""
