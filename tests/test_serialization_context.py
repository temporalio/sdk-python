import dataclasses
import uuid
from typing import Any, Optional, Type

from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from temporalio.converter import (
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    EncodingPayloadConverter,
)
from temporalio.worker import Worker


@workflow.defn
class SerializationContextTestWorkflow:
    @workflow.run
    async def run(self, input: dict[str, Any]) -> dict[str, Any]:
        return input


class SerializationContextTestEncodingPayloadConverter(EncodingPayloadConverter):
    @property
    def encoding(self) -> str:
        return "test-serialization-context"

    def to_payload(self, value: Any) -> Optional[Payload]:
        return None
        # return Payload(
        #     metadata={"encoding": self.encoding.encode()}, data=str(value).encode()
        # )

    def from_payload(self, payload: Payload, type_hint: Optional[Type] = None) -> Any:
        raise RuntimeError("Not implemented")
        # return payload.data.decode()


class SerializationContextTestPayloadConverter(CompositePayloadConverter):
    def __init__(self):
        super().__init__(
            SerializationContextTestEncodingPayloadConverter(),
            *DefaultPayloadConverter.default_encoding_payload_converters,
        )


data_converter = dataclasses.replace(
    DataConverter.default,
    payload_converter_class=SerializationContextTestPayloadConverter,
)


async def test_workflow_payload_conversion_can_be_given_access_to_serialization_context(
    client: Client,
):
    async with Worker(
        client,
        task_queue="test-serialization-context",
        workflows=[SerializationContextTestWorkflow],
        activities=[],
    ):
        result = await client.execute_workflow(
            SerializationContextTestWorkflow.run,
            {},
            id=f"test-serialization-context-{uuid.uuid4()}",
            task_queue="test-serialization-context",
        )

        assert result == {}
