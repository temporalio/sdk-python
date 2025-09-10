from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Type

from typing_extensions import Self

from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from temporalio.converter import (
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    EncodingPayloadConverter,
    SerializationContext,
    WithSerializationContext,
    WorkflowSerializationContext,
)
from temporalio.worker import Worker


@dataclass
class PayloadConverterTraceData:
    to_payload: Optional[WorkflowSerializationContext] = None
    from_payload: Optional[WorkflowSerializationContext] = None


@dataclass
class TraceData:
    workflow_context: PayloadConverterTraceData = field(
        default_factory=PayloadConverterTraceData
    )


@workflow.defn
class SerializationContextTestWorkflow:
    @workflow.run
    async def run(self, input: TraceData) -> TraceData:
        return input


class SerializationContextTestEncodingPayloadConverter(
    EncodingPayloadConverter, WithSerializationContext
):
    def __init__(self):
        self.context: Optional[SerializationContext] = None

    @property
    def encoding(self) -> str:
        return "test-serialization-context"

    def with_context(
        self, context: Optional[SerializationContext]
    ) -> SerializationContextTestEncodingPayloadConverter:
        print(
            f"🌈 SerializationContextTestEncodingPayloadConverter.with_context({context})"
        )
        converter = SerializationContextTestEncodingPayloadConverter()
        converter.context = context
        return converter

    def to_payload(self, value: Any) -> Optional[Payload]:
        value.workflow_context.to_payload = self.context
        return None

    def from_payload(self, payload: Payload, type_hint: Optional[Type] = None) -> Any:
        raise RuntimeError("Not implemented")
        # return payload.data.decode()


class SerializationContextTestPayloadConverter(
    CompositePayloadConverter, WithSerializationContext
):
    def __init__(self):
        super().__init__(
            SerializationContextTestEncodingPayloadConverter(),
            *DefaultPayloadConverter.default_encoding_payload_converters,
        )

    def with_context(self, context: Optional[SerializationContext]) -> Self:
        instance = type(self).__new__(type(self))
        converters = [
            c.with_context(context) if isinstance(c, WithSerializationContext) else c
            for c in self.converters.values()
        ]
        CompositePayloadConverter.__init__(instance, *converters)
        return instance


data_converter = dataclasses.replace(
    DataConverter.default,
    payload_converter_class=SerializationContextTestPayloadConverter,
)


async def test_workflow_payload_conversion_can_be_given_access_to_serialization_context(
    client: Client,
):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    config = client.config()
    config["data_converter"] = data_converter
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SerializationContextTestWorkflow],
        activities=[],
    ):
        result = await client.execute_workflow(
            SerializationContextTestWorkflow.run,
            TraceData(),
            id=workflow_id,
            task_queue=task_queue,
        )

        assert result.workflow_context.to_payload == WorkflowSerializationContext(
            namespace="default",
            workflow_id=workflow_id,
        )
