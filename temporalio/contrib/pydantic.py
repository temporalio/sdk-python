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
import json
from typing import (
    Any,
    Optional,
    Type,
)

import pydantic

import temporalio.workflow
from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    JSONPlainPayloadConverter,
    JSONTypeConverter,
)
from temporalio.worker.workflow_sandbox._restrictions import RestrictionContext

try:
    from pydantic_core import to_jsonable_python
except ImportError:
    from pydantic.json import pydantic_encoder as to_jsonable_python


class _PydanticModelTypeConverter(JSONTypeConverter):
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
            # Pydantic v1
            return model.parse_obj(value)
        else:
            raise ValueError(
                f"{model} is a Pydantic model but does not have a `model_validate` or `parse_obj` method"
            )


class _PydanticJSONPayloadConverter(JSONPlainPayloadConverter):
    """Pydantic JSON payload converter.

    Conversion to JSON is implemented by overriding :py:meth:`to_payload` to use the
    Pydantic encoder.

    Conversion from JSON uses the parent implementation of :py:meth:`from_payload`, with a
    custom type converter. The parent implementation of :py:meth:`from_payload` traverses
    the JSON document according to the structure specified by the type annotation; the
    custom type converter ensures that, during this traversal, Pydantic model instances
    will be created as specified by the type annotation.
    """

    def __init__(self) -> None:
        super().__init__(custom_type_converters=[_PydanticModelTypeConverter()])

    def to_payload(self, value: Any) -> Optional[Payload]:
        """Convert all values with Pydantic encoder or fail.

        Like the base class, we fail if we cannot convert. This payload
        converter is expected to be the last in the chain, so it can fail if
        unable to convert.
        """
        # Let JSON conversion errors be thrown to caller
        return Payload(
            metadata={"encoding": self.encoding.encode()},
            data=json.dumps(
                value, separators=(",", ":"), sort_keys=True, default=to_jsonable_python
            ).encode(),
        )


class _PydanticPayloadConverter(CompositePayloadConverter):
    """Pydantic payload converter.

    Payload converter that replaces the default JSON conversion with Pydantic
    JSON conversion.
    """

    def __init__(self) -> None:
        json_payload_converter = _PydanticJSONPayloadConverter()
        super().__init__(
            *(
                c
                if not isinstance(c, JSONPlainPayloadConverter)
                else json_payload_converter
                for c in DefaultPayloadConverter.default_encoding_payload_converters
            )
        )


pydantic_data_converter = DataConverter(
    payload_converter_class=_PydanticPayloadConverter
)
"""Data converter for Pydantic models.

To use, pass this as the ``data_converter`` argument to :py:class:`temporalio.client.Client`
"""
