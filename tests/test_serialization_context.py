from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
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
from temporalio.worker._workflow_instance import UnsandboxedWorkflowRunner


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


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, data: TraceData) -> TraceData:
        return data


@workflow.defn
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
        print(f"🌈 to_payload({isinstance(value, TraceData)}): {value}")
        if not isinstance(value, TraceData):
            return None
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
        print(f"🌈 from_payload({isinstance(value, TraceData)}): {value}")
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
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
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


# Signal test


@workflow.defn(sandboxed=False)  # so that we can use isinstance
class SignalSerializationContextTestWorkflow:
    def __init__(self) -> None:
        self.signal_received = None

    @workflow.run
    async def run(self) -> TraceData:
        await workflow.wait_condition(lambda: self.signal_received is not None)
        assert self.signal_received is not None
        return self.signal_received

    @workflow.signal
    async def my_signal(self, data: TraceData) -> None:
        self.signal_received = data


async def test_signal_payload_conversion_can_be_given_access_to_serialization_context(
    client: Client,
):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    config = client.config()
    config["data_converter"] = data_converter
    custom_client = Client(**config)

    async with Worker(
        custom_client,
        task_queue=task_queue,
        workflows=[SignalSerializationContextTestWorkflow],
        activities=[],
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
    ):
        handle = await custom_client.start_workflow(
            SignalSerializationContextTestWorkflow.run,
            id=workflow_id,
            task_queue=task_queue,
        )
        await handle.signal(
            SignalSerializationContextTestWorkflow.my_signal,
            TraceData(),
        )
        result = await handle.result()

        workflow_context = dataclasses.asdict(
            WorkflowSerializationContext(
                namespace="default",
                workflow_id=workflow_id,
            )
        )
        assert_trace(
            result.items,
            [
                TraceItem(
                    context_type="workflow",
                    in_workflow=False,
                    method="to_payload",
                    context=workflow_context,  # Outbound signal input
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=False,
                    method="from_payload",
                    context=workflow_context,  # Inbound signal input
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


# Query test


@dataclass
class QueryData:
    query_context: Optional[WorkflowSerializationContext] = None
    value: str = ""


@workflow.defn
class QuerySerializationContextTestWorkflow:
    def __init__(self) -> None:
        self.state = QueryData(value="workflow-state")

    @workflow.run
    async def run(self) -> None:
        # Keep workflow running
        await workflow.wait_condition(lambda: False)

    @workflow.query
    def my_query(self) -> QueryData:
        return self.state


class QuerySerializationContextTestEncodingPayloadConverter(
    EncodingPayloadConverter, WithSerializationContext
):
    def __init__(self, context: Optional[SerializationContext] = None):
        self.context = context

    @property
    def encoding(self) -> str:
        return "test-query-serialization-context"

    def with_context(
        self, context: Optional[SerializationContext]
    ) -> QuerySerializationContextTestEncodingPayloadConverter:
        return QuerySerializationContextTestEncodingPayloadConverter(context)

    trace = []  # Class variable to capture serialization events

    def to_payload(self, value: Any) -> Optional[Payload]:
        # Only handle QueryData objects
        if type(value).__name__ != "QueryData":
            return None

        # Capture the context during serialization
        if self.context and isinstance(self.context, WorkflowSerializationContext):
            self.__class__.trace.append(
                {
                    "operation": "query_result_serialization",
                    "context": self.context,
                    "value": value.value,
                }
            )

        # Serialize as JSON
        data = {"value": value.value}
        return Payload(
            metadata={"encoding": self.encoding.encode()},
            data=json.dumps(data).encode(),
        )

    def from_payload(self, payload: Payload, type_hint: Optional[Type] = None) -> Any:
        data = json.loads(payload.data.decode())
        return QueryData(
            query_context=None,  # Context is not transmitted, only captured during serialization
            value=data.get("value", ""),
        )


class QuerySerializationContextTestPayloadConverter(
    CompositePayloadConverter, WithSerializationContext
):
    def __init__(self, context: Optional[SerializationContext] = None):
        # Create converters with context
        converters = [
            QuerySerializationContextTestEncodingPayloadConverter(context),
            *DefaultPayloadConverter.default_encoding_payload_converters,
        ]
        super().__init__(*converters)
        self.context = context

    def with_context(
        self, context: Optional[SerializationContext]
    ) -> QuerySerializationContextTestPayloadConverter:
        return QuerySerializationContextTestPayloadConverter(context)


async def test_query_payload_conversion_can_be_given_access_to_serialization_context(
    client: Client,
):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    # Clear the trace before starting    QuerySerializationContextTestEncodingPayloadConverter.trace = []

    # Create client with our custom data converter
    data_converter = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=QuerySerializationContextTestPayloadConverter,
    )

    # Create a new client with the custom data converter
    config = client.config()
    config["data_converter"] = data_converter
    custom_client = Client(**config)

    async with Worker(
        custom_client,
        task_queue=task_queue,
        workflows=[QuerySerializationContextTestWorkflow],
        activities=[],
    ):
        # Start the workflow
        handle = await custom_client.start_workflow(
            QuerySerializationContextTestWorkflow.run,
            id=workflow_id,
            task_queue=task_queue,
        )

        # Query the workflow
        result = await handle.query(QuerySerializationContextTestWorkflow.my_query)

        # Verify the result value
        assert result.value == "workflow-state"

        print(
            f"DEBUG: trace length = {len(QuerySerializationContextTestEncodingPayloadConverter.trace)}"
        )
        assert len(QuerySerializationContextTestEncodingPayloadConverter.trace) > 0
        trace_entry = QuerySerializationContextTestEncodingPayloadConverter.trace[-1]
        assert trace_entry["operation"] == "query_result_serialization"
        assert trace_entry["context"] == WorkflowSerializationContext(
            namespace="default",
            workflow_id=workflow_id,
        )
        assert trace_entry["value"] == "workflow-state"

        # Cancel the workflow to clean up
        await handle.cancel()


# Utilities


def assert_trace(trace: list[TraceItem], expected: list[TraceItem]):
    if len(trace) != len(expected):
        raise AssertionError(
            f"expected {len(expected)} trace items but received {len(trace)}"
        )
    history = []
    for item, expected_item in zip_longest(trace, expected):
        if item is None:
            raise AssertionError("Fewer items in trace than expected")
        if expected_item is None:
            raise AssertionError("More items in trace than expected")
        if item != expected_item:
            raise AssertionError(
                f"Item:\n{pformat(item)}\n\ndoes not match expected:\n\n {pformat(expected_item)}.\n\n History:\n{chr(10).join(history)}"
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

        result.append(f"{file_path}:{frame.f_lineno}")

    # Pad with "unknown:0" if we didn't get 3 frames
    while len(result) < 3:
        result.append("unknown:0")

    return result
