from abc import ABC, abstractmethod
from typing import Any, Optional, Tuple

import temporalio.api.common.v1


class PayloadConverter(ABC):
    @abstractmethod
    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        return None

    @abstractmethod
    async def decode(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        return (None, False)

    async def encode_multiple(
        self, values: list[Any]
    ) -> Optional[temporalio.api.common.v1.Payloads]:
        payloads = []
        for value in values:
            payload = await self.encode(value)
            # Return if any payloads cannot be converted
            if payload is None:
                return None
            payloads.append(payload)
        return temporalio.api.common.v1.Payloads(payloads=payloads)

    async def decode_multiple(
        self, payloads: temporalio.api.common.v1.Payloads
    ) -> Tuple[list[Any], bool]:
        values = []
        for payload in payloads.payloads:
            value, ok = await self.decode(payload)
            # Return if any values cannot be converted
            if not ok:
                return ([], False)
            values.append(value)
        return (values, True)


class CompositePayloadConverter(PayloadConverter):
    _converters: list[PayloadConverter]

    def __init__(self, *converters: PayloadConverter) -> None:
        self._converters = list(converters)

    async def encode(self, value: Any) -> Optional[temporalio.api.common.v1.Payload]:
        for converter in self._converters:
            payload = await converter.encode(value)
            if payload is not None:
                return payload
        return None

    async def decode(
        self, payload: temporalio.api.common.v1.Payload
    ) -> Tuple[Any, bool]:
        for converter in self._converters:
            value, ok = await converter.decode(payload)
            if ok:
                return (value, True)
        return (None, False)

    async def encode_multiple(
        self, values: list[Any]
    ) -> Optional[temporalio.api.common.v1.Payloads]:
        for converter in self._converters:
            payloads = await converter.encode_multiple(values)
            if payloads is not None:
                return payloads
        return None

    async def decode_multiple(
        self, payloads: temporalio.api.common.v1.Payloads
    ) -> Tuple[list[Any], bool]:
        for converter in self._converters:
            values, ok = await converter.decode_multiple(payloads)
            if ok:
                return (values, True)
        return ([], False)
