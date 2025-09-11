from __future__ import annotations

import dataclasses
import inspect
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, Optional, Type

from temporalio import activity, workflow
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
    in_workflow: bool
    caller_location: list[str] = field(default_factory=list)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TraceItem):
            return False
        return (
            self.context_type == other.context_type
            and self.method == other.method
            and self.context == other.context
            and self.in_workflow == other.in_workflow
        )


@dataclass
class TraceData:
    items: list[TraceItem] = field(default_factory=list)


@activity.defn
async def passthrough_activity(input: TraceData) -> TraceData:
    return input


@workflow.defn(sandboxed=False)  # we want to use isinstance
class SerializationContextTestWorkflow:
    @workflow.run
    async def run(self, input: TraceData) -> TraceData:
        return await workflow.execute_activity(
            passthrough_activity,
            input,
            start_to_close_timeout=timedelta(seconds=10),
        )


def get_caller_location() -> list[str]:
    """Get 3 stack frames starting from the first that's not in test_serialization_context.py or temporalio/converter.py."""
    frame = inspect.currentframe()
    result = []
    found_first = False

    # Walk up the stack
    while frame and len(result) < 3:
        frame = frame.f_back
        if not frame:
            break

        file_path = frame.f_code.co_filename

        # Skip frames from test file and converter.py until we find the first one
        if not found_first:
            if "test_serialization_context.py" in file_path:
                continue
            if file_path.endswith("temporalio/converter.py"):
                continue
            found_first = True

        # Format and add this frame
        line_number = frame.f_lineno
        display_path = file_path
        if "/sdk-python/" in display_path:
            display_path = display_path.split("/sdk-python/")[-1]
        result.append(f"{display_path}:{line_number}")

    # Pad with "unknown:0" if we didn't get 3 frames
    while len(result) < 3:
        result.append("unknown:0")

    return result


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
        converter = SerializationContextTestEncodingPayloadConverter()
        converter.context = context
        return converter

    def to_payload(self, value: Any) -> Optional[Payload]:
        assert isinstance(value, TraceData)
        assert isinstance(self.context, WorkflowSerializationContext)
        value.items.append(
            TraceItem(
                context_type="workflow",
                in_workflow=workflow.in_workflow(),
                method="to_payload",
                context=self.context,
                caller_location=get_caller_location(),
            )
        )
        payload = JSONPlainPayloadConverter().to_payload(value)
        assert payload
        payload.metadata["encoding"] = self.encoding.encode()
        return payload

    def from_payload(self, payload: Payload, type_hint: Optional[Type] = None) -> Any:
        value = JSONPlainPayloadConverter().from_payload(payload, type_hint)
        assert isinstance(value, TraceData)
        assert isinstance(self.context, WorkflowSerializationContext)
        value.items.append(
            TraceItem(
                context_type="workflow",
                in_workflow=workflow.in_workflow(),
                method="from_payload",
                context=self.context,
                caller_location=get_caller_location(),
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
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    config = client.config()
    config["data_converter"] = data_converter
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SerializationContextTestWorkflow],
        activities=[passthrough_activity],
    ):
        result = await client.execute_workflow(
            SerializationContextTestWorkflow.run,
            TraceData(),
            id=workflow_id,
            task_queue=task_queue,
        )

        workflow_context = WorkflowSerializationContext(
            namespace="default",
            workflow_id=workflow_id,
        )
        assert result.items == [
            TraceItem(
                context_type="workflow",
                in_workflow=False,
                method="to_payload",
                context=workflow_context,
            ),
            TraceItem(
                context_type="workflow",
                in_workflow=False,
                method="from_payload",
                context=workflow_context,
            ),
            TraceItem(
                context_type="workflow",
                in_workflow=True,
                method="to_payload",
                context=workflow_context,
            ),
            TraceItem(
                context_type="workflow",
                in_workflow=False,
                method="from_payload",
                context=workflow_context,
            ),
        ]
