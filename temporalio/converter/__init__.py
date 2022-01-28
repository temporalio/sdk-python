from .converter import CompositePayloadConverter, PayloadConverter
from .plain import (
    BinaryNullPayloadConverter,
    BinaryPlainPayloadConverter,
    JSONPlainPayloadConverter,
)
from .proto import BinaryProtoPayloadConverter, JSONProtoPayloadConverter


# TODO(cretz): Should this be a var that can be changed instead? If so, can it
# be replaced _after_ client creation? We'd just have to fallback to this
# default at conversion time instead of instantiation time.
def default() -> PayloadConverter:
    return CompositePayloadConverter(
        BinaryNullPayloadConverter(),
        BinaryPlainPayloadConverter(),
        JSONProtoPayloadConverter(),
        BinaryProtoPayloadConverter(),
        JSONPlainPayloadConverter(),
    )
