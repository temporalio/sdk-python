from typing import Any, Optional, Tuple

import google.protobuf.json_format
import google.protobuf.message
import google.protobuf.symbol_database

import temporalio.api.common.v1
import temporalio.converter

_sym_db = google.protobuf.symbol_database.Default()


class JSONProtoPayloadConverter(temporalio.converter.PayloadConverter):
    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        if issubclass(value, google.protobuf.message.Message):
            return temporalio.api.common.v1.Payload(
                metadata={
                    "encoding": b"json/protobuf",
                    "messageType": value.DESCRIPTOR.full_name,
                },
                data=google.protobuf.json_format.MessageToJson(value).encode(),
            )
        return None

    async def decode(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        if payload.metadata["encoding"] == b"json/protobuf":
            # This raises error if not found
            value = _sym_db.GetSymbol(str(payload.metadata["messageType"]))()
            google.protobuf.json_format.Parse(payload.data, value)
            return (value, True)
        return (None, False)


class BinaryProtoPayloadConverter(temporalio.converter.PayloadConverter):
    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        if issubclass(value, google.protobuf.message.Message):
            return temporalio.api.common.v1.Payload(
                metadata={
                    "encoding": b"binary/protobuf",
                    "messageType": value.DESCRIPTOR.full_name,
                },
                data=value.SerializeToString(),
            )
        return None

    async def decode(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        if payload.metadata["encoding"] == b"binary/protobuf":
            # This raises error if not found
            value = _sym_db.GetSymbol(str(payload.metadata["messageType"]))()
            value.ParseFromString(payload.data)
            return (value, True)
        return (None, False)
