from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Type

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
class WorkflowData:
    workflow_context: Optional[WorkflowSerializationContext] = None


@workflow.defn
class SerializationContextTestWorkflow:
    @workflow.run
    async def run(self, input: WorkflowData) -> WorkflowData:
        return input


class SerializationContextTestEncodingPayloadConverter(
    EncodingPayloadConverter, WithSerializationContext
):
    def __init__(self, context: Optional[SerializationContext]):
        self.context = context

    @property
    def encoding(self) -> str:
        return "test-serialization-context"

    def with_context(
        self, context: Optional[SerializationContext]
    ) -> SerializationContextTestEncodingPayloadConverter:
        return SerializationContextTestEncodingPayloadConverter(context)

    def to_payload(self, value: Any) -> Optional[Payload]:
        assert isinstance(value, WorkflowData)
        assert isinstance(self.context, WorkflowSerializationContext)
        value.workflow_context = self.context
        return None

    def from_payload(self, payload: Payload, type_hint: Optional[Type] = None) -> Any:
        raise RuntimeError("Not implemented")
        # return payload.data.decode()


class SerializationContextTestPayloadConverter(CompositePayloadConverter):
    def __init__(self):
        super().__init__(
            SerializationContextTestEncodingPayloadConverter(None),
            *DefaultPayloadConverter.default_encoding_payload_converters,
        )


data_converter = dataclasses.replace(
    DataConverter.default,
    payload_converter_class=SerializationContextTestPayloadConverter,
)


async def test_workflow_payload_conversion_can_be_given_access_to_serialization_context(
    client: Client,
):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SerializationContextTestWorkflow],
        activities=[],
    ):
        result = await client.execute_workflow(
            SerializationContextTestWorkflow.run,
            WorkflowData(),
            id=workflow_id,
            task_queue=task_queue,
        )

        assert result.workflow_context == WorkflowSerializationContext(
            namespace="default",
            workflow_id=workflow_id,
        )
