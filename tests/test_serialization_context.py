from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Type

from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from temporalio.converter import (
    ActivitySerializationContext,
    CompositePayloadConverter,
    DataConverter,
    DefaultPayloadConverter,
    EncodingPayloadConverter,
    JSONPlainPayloadConverter,
    SerializationContext,
    WithSerializationContext,
    WorkflowSerializationContext,
)
from temporalio.worker import Worker


@dataclass
class TraceItem:
    context_type: Literal["workflow", "activity"]
    method: Literal["to_payload", "from_payload"]
    context: WorkflowSerializationContext | ActivitySerializationContext


@dataclass
class TraceData:
    items: list[TraceItem] = field(default_factory=list)


@workflow.defn(sandboxed=False)  # we want to use isinstance
class PassThroughWorkflow:
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
        print(
            f"🌈 SerializationContextTestEncodingPayloadConverter.to_payload({value})"
        )
        assert isinstance(value, TraceData)
        assert isinstance(self.context, WorkflowSerializationContext)
        value.items.append(
            TraceItem(
                context_type="workflow", method="to_payload", context=self.context
            )
        )
        payload = JSONPlainPayloadConverter().to_payload(value)
        assert payload
        payload.metadata["encoding"] = self.encoding.encode()
        return payload

    def from_payload(self, payload: Payload, type_hint: Optional[Type] = None) -> Any:
        print(
            f"🌈 SerializationContextTestEncodingPayloadConverter.from_payload({payload}, {type_hint})"
        )
        value = JSONPlainPayloadConverter().from_payload(payload, type_hint)
        assert isinstance(value, TraceData)
        assert isinstance(self.context, WorkflowSerializationContext)
        value.items.append(
            TraceItem(
                context_type="workflow", method="from_payload", context=self.context
            )
        )
        return value


class SerializationContextTestPayloadConverter(
    CompositePayloadConverter, WithSerializationContext
):
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
    print()
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    config = client.config()
    config["data_converter"] = data_converter
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[PassThroughWorkflow],
        activities=[],
    ):
        result = await client.execute_workflow(
            PassThroughWorkflow.run,
            TraceData(),
            id=workflow_id,
            task_queue=task_queue,
        )

        workflow_context = WorkflowSerializationContext(
            namespace="default",
            workflow_id=workflow_id,
        )
        for item in result.items:
            print(item)
