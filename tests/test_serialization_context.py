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
from warnings import warn

import pytest

from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client, WorkflowUpdateFailedError
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


async def test_workflow_payload_conversion(
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


async_activity_started = asyncio.Event()


# Async activity completion test
@activity.defn
async def async_activity() -> TraceData:
    async_activity_started.set()
    activity.raise_complete_async()


@workflow.defn
class AsyncActivityCompletionSerializationContextTestWorkflow:
    @workflow.run
    async def run(self) -> TraceData:
        return await workflow.execute_activity(
            async_activity,
            start_to_close_timeout=timedelta(seconds=10),
            activity_id="async-activity-id",
        )


async def test_async_activity_completion_payload_conversion(
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
        workflows=[AsyncActivityCompletionSerializationContextTestWorkflow],
        activities=[async_activity],
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
    ):
        wf_handle = await client.start_workflow(
            AsyncActivityCompletionSerializationContextTestWorkflow.run,
            id=workflow_id,
            task_queue=task_queue,
        )
        activity_handle = client.get_async_activity_handle(
            workflow_id=workflow_id,
            run_id=wf_handle.first_execution_run_id,
            activity_id="async-activity-id",
        )
        await async_activity_started.wait()
        data = TraceData()
        await activity_handle.heartbeat(data)
        await activity_handle.complete(data)
        result = await wf_handle.result()

        # project down since activity completion by a client does not have access to most activity
        # context fields
        def project(trace_item: TraceItem) -> tuple[str, bool, str]:
            return (
                trace_item.context_type,
                trace_item.in_workflow,
                trace_item.method,
            )

        assert [project(item) for item in result.items] == [
            (
                "activity",
                False,
                "to_payload",  # Outbound activity input
            ),
            (
                "activity",
                False,
                "to_payload",  # Outbound activity heartbeat data
            ),
            (
                "activity",
                False,
                "from_payload",  # Inbound activity result
            ),
            (
                "workflow",
                True,
                "to_payload",  # Outbound workflow result
            ),
            (
                "workflow",
                False,
                "from_payload",  # Inbound workflow result
            ),
        ]


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


async def test_signal_payload_conversion(
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


@workflow.defn
class QuerySerializationContextTestWorkflow:
    @workflow.run
    async def run(self) -> None:
        await asyncio.Event().wait()

    @workflow.query
    def my_query(self, input: TraceData) -> TraceData:
        return input


async def test_query_payload_conversion(
    client: Client,
):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    data_converter = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextTestPayloadConverter,
    )

    config = client.config()
    config["data_converter"] = data_converter
    custom_client = Client(**config)

    async with Worker(
        custom_client,
        task_queue=task_queue,
        workflows=[QuerySerializationContextTestWorkflow],
        activities=[],
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
    ):
        handle = await custom_client.start_workflow(
            QuerySerializationContextTestWorkflow.run,
            id=workflow_id,
            task_queue=task_queue,
        )
        result = await handle.query(
            QuerySerializationContextTestWorkflow.my_query, TraceData()
        )

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
                    context=workflow_context,  # Outbound query input
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=True,
                    method="from_payload",
                    context=workflow_context,  # Inbound query input
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=True,
                    method="to_payload",
                    context=workflow_context,  # Outbound query result
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=False,
                    method="from_payload",
                    context=workflow_context,  # Inbound query result
                ),
            ],
        )


# Update test


@workflow.defn
class UpdateSerializationContextTestWorkflow:
    @workflow.init
    def __init__(self, pass_validation: bool) -> None:
        self.pass_validation = pass_validation
        self.input = None

    @workflow.run
    async def run(self, pass_validation: bool) -> TraceData:
        await workflow.wait_condition(lambda: self.input is not None)
        assert self.input
        return self.input

    @workflow.update
    def my_update(self, input: TraceData) -> TraceData:
        return input

    @my_update.validator
    def my_update_validator(self, input: TraceData) -> None:
        self.input = input  # for test purposes; update validators should not mutate workflow state
        if not self.pass_validation:
            raise ValueError("Rejected")


@pytest.mark.parametrize("pass_validation", [True, False])
async def test_update_payload_conversion(
    client: Client,
    pass_validation: bool,
):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    data_converter = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextTestPayloadConverter,
    )

    config = client.config()
    config["data_converter"] = data_converter
    custom_client = Client(**config)

    async with Worker(
        custom_client,
        task_queue=task_queue,
        workflows=[UpdateSerializationContextTestWorkflow],
        activities=[],
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
    ):
        wf_handle = await custom_client.start_workflow(
            UpdateSerializationContextTestWorkflow.run,
            pass_validation,
            id=workflow_id,
            task_queue=task_queue,
        )
        if pass_validation:
            result = await wf_handle.execute_update(
                UpdateSerializationContextTestWorkflow.my_update, TraceData()
            )
        else:
            try:
                await wf_handle.execute_update(
                    UpdateSerializationContextTestWorkflow.my_update, TraceData()
                )
                raise AssertionError("Expected WorkflowUpdateFailedError")
            except WorkflowUpdateFailedError:
                pass

            result = await wf_handle.result()

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
                    context=workflow_context,  # Outbound update input
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=True,
                    method="from_payload",
                    context=workflow_context,  # Inbound update input
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=True,
                    method="to_payload",
                    context=workflow_context,  # Outbound update/workflow result
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=False,
                    method="from_payload",
                    context=workflow_context,  # Inbound update/workflow result
                ),
            ],
        )


# External workflow test


@workflow.defn
class ExternalWorkflowTarget:
    def __init__(self) -> None:
        self.signal_received = None

    @workflow.run
    async def run(self) -> TraceData:
        try:
            # Wait for signal
            await workflow.wait_condition(lambda: self.signal_received is not None)
            return self.signal_received or TraceData()
        except asyncio.CancelledError:
            # Return empty data on cancellation
            return TraceData()

    @workflow.signal
    async def external_signal(self, data: TraceData) -> None:
        self.signal_received = data


@workflow.defn
class ExternalWorkflowSignaler:
    @workflow.run
    async def run(self, target_id: str, data: TraceData) -> TraceData:
        # Signal external workflow
        handle = workflow.get_external_workflow_handle(target_id)
        await handle.signal(ExternalWorkflowTarget.external_signal, data)
        return data


@workflow.defn
class ExternalWorkflowCanceller:
    @workflow.run
    async def run(self, target_id: str) -> TraceData:
        # Cancel external workflow
        handle = workflow.get_external_workflow_handle(target_id)
        await handle.cancel()
        return TraceData()


@pytest.mark.timeout(10)
async def test_external_workflow_signal_and_cancel_payload_conversion(
    client: Client,
):
    target_workflow_id = str(uuid.uuid4())
    signaler_workflow_id = str(uuid.uuid4())
    canceller_workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    data_converter = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextTestPayloadConverter,
    )

    config = client.config()
    config["data_converter"] = data_converter
    custom_client = Client(**config)

    async with Worker(
        custom_client,
        task_queue=task_queue,
        workflows=[
            ExternalWorkflowTarget,
            ExternalWorkflowSignaler,
            ExternalWorkflowCanceller,
        ],
        activities=[],
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
    ):
        # Test external signal
        target_handle = await custom_client.start_workflow(
            ExternalWorkflowTarget.run,
            id=target_workflow_id,
            task_queue=task_queue,
        )

        signaler_handle = await custom_client.start_workflow(
            ExternalWorkflowSignaler.run,
            args=[target_workflow_id, TraceData()],
            id=signaler_workflow_id,
            task_queue=task_queue,
        )

        # Wait for both to complete
        signaler_result = await signaler_handle.result()
        target_result = await target_handle.result()

        # Verify signal trace
        signaler_context = dataclasses.asdict(
            WorkflowSerializationContext(
                namespace="default",
                workflow_id=signaler_workflow_id,
            )
        )
        target_context = dataclasses.asdict(
            WorkflowSerializationContext(
                namespace="default",
                workflow_id=target_workflow_id,
            )
        )

        # This test verifies that external signals SHOULD use the target workflow's context
        # This is the DESIRED behavior to match .NET
        assert_trace(
            signaler_result.items,
            [
                TraceItem(
                    context_type="workflow",
                    in_workflow=False,
                    method="to_payload",
                    context=signaler_context,  # Outbound signaler workflow input
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=False,
                    method="from_payload",
                    context=signaler_context,  # Inbound signaler workflow input
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=True,
                    method="to_payload",
                    context=target_context,  # Should use target workflow's context for external signal
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=True,
                    method="to_payload",
                    context=signaler_context,  # Outbound signaler workflow result
                ),
                TraceItem(
                    context_type="workflow",
                    in_workflow=False,
                    method="from_payload",
                    context=signaler_context,  # Inbound signaler workflow result
                ),
            ],
        )

        # Note: External cancel doesn't send payloads, so we don't test it here
        # The cancel context would only be used for failure deserialization


# Utilities


def assert_trace(trace: list[TraceItem], expected: list[TraceItem]):
    if len(trace) != len(expected):
        warn(f"expected {len(expected)} trace items but received {len(trace)}")
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
