from __future__ import annotations

import asyncio
import dataclasses
import inspect
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import zip_longest
from pprint import pformat, pprint
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
    context: dict[str, Any]
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
    activity.heartbeat(input)
    # Wait for the heartbeat to be processed so that it modifies the data before the activity returns
    await asyncio.sleep(0.2)
    return input


@workflow.defn(sandboxed=False)
class EchoWorkflow:
    @workflow.run
    async def run(self, data: TraceData) -> TraceData:
        return data


@workflow.defn(sandboxed=False)  # we want to use isinstance
class SerializationContextTestWorkflow:
    @workflow.run
    async def run(self, data: TraceData) -> TraceData:
        data = await workflow.execute_activity(
            passthrough_activity,
            data,
            start_to_close_timeout=timedelta(seconds=10),
            heartbeat_timeout=timedelta(seconds=2),
        )
        data = await workflow.execute_child_workflow(
            EchoWorkflow.run, data, id=f"{workflow.info().workflow_id}_child"
        )
        return data


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
        if not self.context:
            raise Exception("Context is None")
        if isinstance(self.context, WorkflowSerializationContext):
            value.items.append(
                TraceItem(
                    context_type="workflow",
                    in_workflow=workflow.in_workflow(),
                    method="to_payload",
                    context=dataclasses.asdict(self.context),
                    caller_location=get_caller_location(),
                )
            )
        elif isinstance(self.context, ActivitySerializationContext):
            value.items.append(
                TraceItem(
                    context_type="activity",
                    in_workflow=workflow.in_workflow(),
                    method="to_payload",
                    context=dataclasses.asdict(self.context),
                    caller_location=get_caller_location(),
                )
            )
        else:
            raise Exception(f"Unexpected context type: {type(self.context)}")
        payload = JSONPlainPayloadConverter().to_payload(value)
        assert payload
        payload.metadata["encoding"] = self.encoding.encode()
        return payload

    def from_payload(self, payload: Payload, type_hint: Optional[Type] = None) -> Any:
        value = JSONPlainPayloadConverter().from_payload(payload, type_hint)
        assert isinstance(value, TraceData)
        if not self.context:
            raise Exception("Context is None")
        if isinstance(self.context, WorkflowSerializationContext):
            value.items.append(
                TraceItem(
                    context_type="workflow",
                    in_workflow=workflow.in_workflow(),
                    method="from_payload",
                    context=dataclasses.asdict(self.context),
                    caller_location=get_caller_location(),
                )
            )
        elif isinstance(self.context, ActivitySerializationContext):
            value.items.append(
                TraceItem(
                    context_type="activity",
                    in_workflow=workflow.in_workflow(),
                    method="from_payload",
                    context=dataclasses.asdict(self.context),
                    caller_location=get_caller_location(),
                )
            )
        else:
            raise Exception(f"Unexpected context type: {type(self.context)}")
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
        workflows=[SerializationContextTestWorkflow, EchoWorkflow],
        activities=[passthrough_activity],
    ):
        result = await client.execute_workflow(
            SerializationContextTestWorkflow.run,
            TraceData(),
            id=workflow_id,
            task_queue=task_queue,
        )

        workflow_context = dataclasses.asdict(
            WorkflowSerializationContext(
                namespace="default",
                workflow_id=workflow_id,
            )
        )
        child_workflow_context = dataclasses.asdict(
            WorkflowSerializationContext(
                namespace="default",
                workflow_id=f"{workflow_id}_child",
            )
        )
        activity_context = dataclasses.asdict(
            ActivitySerializationContext(
                namespace="default",
                workflow_id=workflow_id,
                workflow_type="SerializationContextTestWorkflow",
                activity_type="passthrough_activity",
                activity_task_queue=task_queue,
                is_local=False,
            )
        )
        if True:
            assert_trace(
                result.items,
                [
                    TraceItem(
                        context_type="workflow",
                        in_workflow=False,
                        method="to_payload",
                        context=workflow_context,  # Outbound workflow input
                    ),
                    TraceItem(
                        context_type="workflow",
                        in_workflow=False,
                        method="from_payload",
                        context=workflow_context,  # Inbound workflow input
                    ),
                    TraceItem(
                        context_type="activity",
                        in_workflow=True,
                        method="to_payload",
                        context=activity_context,  # Outbound activity input
                    ),
                    TraceItem(
                        context_type="activity",
                        in_workflow=False,
                        method="from_payload",
                        context=activity_context,  # Inbound activity input
                    ),
                    TraceItem(
                        context_type="activity",
                        in_workflow=False,
                        method="to_payload",
                        context=activity_context,  # Outbound heartbeat
                    ),
                    TraceItem(
                        context_type="activity",
                        in_workflow=False,
                        method="to_payload",
                        context=activity_context,  # Outbound activity result
                    ),
                    TraceItem(
                        context_type="activity",
                        in_workflow=False,
                        method="from_payload",
                        context=activity_context,  # Inbound activity result
                    ),
                    TraceItem(
                        context_type="workflow",
                        in_workflow=True,
                        method="to_payload",
                        context=child_workflow_context,  # Outbound child workflow input
                    ),
                    TraceItem(
                        context_type="workflow",
                        in_workflow=False,
                        method="from_payload",
                        context=child_workflow_context,  # Inbound child workflow input
                    ),
                    TraceItem(
                        context_type="workflow",
                        in_workflow=True,
                        method="to_payload",
                        context=child_workflow_context,  # Outbound child workflow result
                    ),
                    TraceItem(
                        context_type="workflow",
                        in_workflow=False,
                        method="from_payload",
                        context=child_workflow_context,  # Inbound child workflow result
                    ),
                    TraceItem(
                        context_type="workflow",
                        in_workflow=True,
                        method="to_payload",
                        context=workflow_context,  # Outbound workflow result
                    ),
                    TraceItem(
                        context_type="workflow",
                        in_workflow=False,
                        method="from_payload",
                        context=workflow_context,  # Inbound workflow result
                    ),
                ],
            )
        else:
            pprint(result.items)


def assert_trace(trace: list[TraceItem], expected: list[TraceItem]):
    history = []
    for item, expected_item in zip_longest(trace, expected):
        if item is None:
            raise AssertionError("Fewer items in trace than expected")
        if expected_item is None:
            raise AssertionError("More items in trace than expected")
        if item != expected_item:
            raise AssertionError(
                f"Item:\n{pformat(item)}\n\ndoes not match expected:\n\n {pformat(expected_item)}.\n\n History:\n{'\n'.join(history)}"
            )
        history.append(f"{item.context_type} {item.method}")


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
