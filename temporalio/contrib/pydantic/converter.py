import inspect
import json
from typing import (
    Any,
    Optional,
    Type,
)

import pydantic
from pydantic.json import pydantic_encoder

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


class PydanticJSONPayloadConverter(JSONPlainPayloadConverter):
    """Pydantic JSON payload converter.

    Extends :py:class:`JSONPlainPayloadConverter` to override :py:meth:`to_payload` using
    the Pydantic encoder. :py:meth:`from_payload` uses the parent implementation, with a
    custom type converter.
    """

    def __init__(self) -> None:
        super().__init__(custom_type_converters=[PydanticModelTypeConverter()])

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
                value, separators=(",", ":"), sort_keys=True, default=pydantic_encoder
            ).encode(),
        )


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
            # Pydantic v1
            return model.parse_obj(value)
        else:
            raise ValueError(
                f"{model} is a Pydantic model but does not have a `model_validate` or `parse_obj` method"
            )


class PydanticPayloadConverter(CompositePayloadConverter):
    """Payload converter that replaces Temporal JSON conversion with Pydantic
    JSON conversion.
    """

    def __init__(self) -> None:
        json_payload_converter = PydanticJSONPayloadConverter()
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
"""Data converter using Pydantic JSON conversion."""
