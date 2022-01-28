import dataclasses
import json
from dataclasses import dataclass
from typing import Any, Optional, Tuple, Type

import temporalio.api.common.v1
import temporalio.converter


class BinaryNullPayloadConverter(temporalio.converter.PayloadConverter):
    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        if value is None:
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": b"binary/null"}
            )
        return None

    async def decode(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        return (None, payload.metadata["encoding"] == b"binary/null")


class BinaryPlainPayloadConverter(temporalio.converter.PayloadConverter):
    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        if isinstance(value, bytes):
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": b"binary/plain"}, data=value
            )
        return None

    async def decode(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        return (payload.data, payload.metadata["encoding"] == b"binary/plain")


class JSONPlainPayloadConverter(temporalio.converter.PayloadConverter):
    _encoder: Optional[Type[json.JSONEncoder]]
    _decoder: Optional[Type[json.JSONDecoder]]
    _dataclass_asdict: bool
    _encoding: bytes

    # TODO(cretz): Document that it can be customized/reused, but the encoding should be changed
    def __init__(
        self,
        *,
        encoder: Optional[Type[json.JSONEncoder]] = None,
        decoder: Optional[Type[json.JSONDecoder]] = None,
        dataclass_asdict: bool = True,
        encoding: str = "json/plain"
    ) -> None:
        super().__init__()
        self._encoder = encoder
        self._decoder = decoder
        self._dataclass_asdict = dataclass_asdict
        self._encoding = encoding.encode()

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        if self._dataclass_asdict and dataclasses.is_dataclass(value):
            value = dataclasses.asdict(value)
        # We swallow JSON encode error and just return None
        try:
            return temporalio.api.common.v1.Payload(
                metadata={"encoding": self._encoding},
                data=json.dumps(value, cls=self._encoder).encode(),
            )
        except (RuntimeError, TypeError, ValueError):
            return None

    async def decode(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        if payload.metadata["encoding"] == self._encoding:
            # We do not swallow JSON decode errors since we expect success due
            # to already-matched encoding
            return (json.loads(payload.data, cls=self._decoder), True)
        return (None, False)
