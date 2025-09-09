"""
Test context-aware serde/codec operations.

Serialization context should be available on all serde/codec operations, but testing all of them is
infeasible; this test suite only covers a selection.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, List, Literal, Optional, Sequence, Type

import pytest
from pydantic import BaseModel
from typing_extensions import Never

from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload
from temporalio.api.failure.v1 import Failure
from temporalio.client import Client, WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.common import RetryPolicy
from temporalio.contrib.pydantic import PydanticJSONPlainPayloadConverter
from temporalio.converter import (
    ActivitySerializationContext,
    CompositePayloadConverter,
    DataConverter,
    DefaultFailureConverter,
    DefaultPayloadConverter,
    EncodingPayloadConverter,
    JSONPlainPayloadConverter,
    PayloadCodec,
    PayloadConverter,
    SerializationContext,
    WithSerializationContext,
    WorkflowSerializationContext,
)
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker
from temporalio.worker._workflow_instance import UnsandboxedWorkflowRunner


@dataclass
class TraceItem:
    method: Literal[
        "to_payload",
        "from_payload",
        "to_failure",
        "from_failure",
        "encode",
        "decode",
    ]
    context: dict[str, Any]


@dataclass
class TraceData:
    items: list[TraceItem] = field(default_factory=list)


class SerializationContextPayloadConverter(
    EncodingPayloadConverter, WithSerializationContext
):
    def __init__(self):
        self.context: Optional[SerializationContext] = None

    @property
    def encoding(self) -> str:
        return "test-serialization-context"

    def with_context(
        self, context: Optional[SerializationContext]
    ) -> SerializationContextPayloadConverter:
        converter = SerializationContextPayloadConverter()
        converter.context = context
        return converter

    def to_payload(self, value: Any) -> Optional[Payload]:
        if not isinstance(value, TraceData):
            return None
        if not self.context:
            raise Exception("Context is None")
        if isinstance(self.context, WorkflowSerializationContext):
            value.items.append(
                TraceItem(
                    method="to_payload",
                    context=dataclasses.asdict(self.context),
                )
            )
        elif isinstance(self.context, ActivitySerializationContext):
            value.items.append(
                TraceItem(
                    method="to_payload",
                    context=dataclasses.asdict(self.context),
                )
            )
        else:
            raise Exception(f"Unexpected context type: {type(self.context)}")
        payload = JSONPlainPayloadConverter().to_payload(value)
        assert payload
        payload.metadata["encoding"] = self.encoding.encode()
        return payload

    def from_payload(self, payload: Payload, type_hint: Optional[Type] = None) -> Any:
        # Always deserialize as TraceData since that's what this converter handles
        value = JSONPlainPayloadConverter().from_payload(payload, TraceData)
        assert isinstance(value, TraceData)
        if not self.context:
            raise Exception("Context is None")
        if isinstance(self.context, WorkflowSerializationContext):
            value.items.append(
                TraceItem(
                    method="from_payload",
                    context=dataclasses.asdict(self.context),
                )
            )
        elif isinstance(self.context, ActivitySerializationContext):
            value.items.append(
                TraceItem(
                    method="from_payload",
                    context=dataclasses.asdict(self.context),
                )
            )
        else:
            raise Exception(f"Unexpected context type: {type(self.context)}")
        return value


class SerializationContextCompositePayloadConverter(
    CompositePayloadConverter, WithSerializationContext
):
    def __init__(self):
        super().__init__(
            SerializationContextPayloadConverter(),
            *DefaultPayloadConverter.default_encoding_payload_converters,
        )


# Test payload conversion


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
class PayloadConversionWorkflow:
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


async def test_workflow_payload_conversion(
    client: Client,
):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    config = client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextCompositePayloadConverter,
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[PayloadConversionWorkflow, EchoWorkflow],
        activities=[passthrough_activity],
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
    ):
        result = await client.execute_workflow(
            PayloadConversionWorkflow.run,
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
                workflow_type=PayloadConversionWorkflow.__name__,
                activity_type=passthrough_activity.__name__,
                activity_task_queue=task_queue,
                is_local=False,
            )
        )
        assert result.items == [
            TraceItem(
                method="to_payload",
                context=workflow_context,  # Outbound workflow input
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context,  # Inbound workflow input
            ),
            TraceItem(
                method="to_payload",
                context=activity_context,  # Outbound activity input
            ),
            TraceItem(
                method="from_payload",
                context=activity_context,  # Inbound activity input
            ),
            TraceItem(
                method="to_payload",
                context=activity_context,  # Outbound heartbeat
            ),
            TraceItem(
                method="to_payload",
                context=activity_context,  # Outbound activity result
            ),
            TraceItem(
                method="from_payload",
                context=activity_context,  # Inbound activity result
            ),
            TraceItem(
                method="to_payload",
                context=child_workflow_context,  # Outbound child workflow input
            ),
            TraceItem(
                method="from_payload",
                context=child_workflow_context,  # Inbound child workflow input
            ),
            TraceItem(
                method="to_payload",
                context=child_workflow_context,  # Outbound child workflow result
            ),
            TraceItem(
                method="from_payload",
                context=child_workflow_context,  # Inbound child workflow result
            ),
            TraceItem(
                method="to_payload",
                context=workflow_context,  # Outbound workflow result
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context,  # Inbound workflow result
            ),
        ]


# Activity with heartbeat details test


@activity.defn
async def activity_with_heartbeat_details() -> TraceData:
    """Activity that checks heartbeat details are decoded with proper context."""
    info = activity.info()

    if info.heartbeat_details:
        assert len(info.heartbeat_details) == 1
        heartbeat_data = info.heartbeat_details[0]
        assert isinstance(heartbeat_data, TraceData)
        return heartbeat_data

    data = TraceData()
    activity.heartbeat(data)
    await asyncio.sleep(0.1)
    raise Exception("Intentional failure to test heartbeat details")


@workflow.defn
class HeartbeatDetailsSerializationContextTestWorkflow:
    @workflow.run
    async def run(self) -> TraceData:
        return await workflow.execute_activity(
            activity_with_heartbeat_details,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=100),
                maximum_attempts=2,
            ),
        )


async def test_heartbeat_details_payload_conversion(client: Client):
    """Test that heartbeat details are decoded with activity context."""
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    config = client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextCompositePayloadConverter,
    )

    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[HeartbeatDetailsSerializationContextTestWorkflow],
        activities=[activity_with_heartbeat_details],
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
    ):
        result = await client.execute_workflow(
            HeartbeatDetailsSerializationContextTestWorkflow.run,
            id=workflow_id,
            task_queue=task_queue,
        )

        activity_context = dataclasses.asdict(
            ActivitySerializationContext(
                namespace="default",
                workflow_id=workflow_id,
                workflow_type=HeartbeatDetailsSerializationContextTestWorkflow.__name__,
                activity_type=activity_with_heartbeat_details.__name__,
                activity_task_queue=task_queue,
                is_local=False,
            )
        )

        found_heartbeat_decode = False
        for item in result.items:
            if item.method == "from_payload" and item.context == activity_context:
                found_heartbeat_decode = True
                break

        assert (
            found_heartbeat_decode
        ), "Heartbeat details should be decoded with activity context"


# Local activity test


@activity.defn
async def local_activity(input: TraceData) -> TraceData:
    return input


@workflow.defn
class LocalActivityWorkflow:
    @workflow.run
    async def run(self, data: TraceData) -> TraceData:
        return await workflow.execute_local_activity(
            local_activity,
            data,
            start_to_close_timeout=timedelta(seconds=10),
        )


async def test_local_activity_payload_conversion(client: Client):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    config = client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextCompositePayloadConverter,
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[LocalActivityWorkflow],
        activities=[local_activity],
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
    ):
        result = await client.execute_workflow(
            LocalActivityWorkflow.run,
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
        local_activity_context = dataclasses.asdict(
            ActivitySerializationContext(
                namespace="default",
                workflow_id=workflow_id,
                workflow_type=LocalActivityWorkflow.__name__,
                activity_type=local_activity.__name__,
                activity_task_queue=task_queue,
                is_local=True,
            )
        )

        assert (
            result.items
            == [
                TraceItem(
                    method="to_payload",
                    context=workflow_context,  # Outbound workflow input
                ),
                TraceItem(
                    method="from_payload",
                    context=workflow_context,  # Inbound workflow input
                ),
                TraceItem(
                    method="to_payload",
                    context=local_activity_context,  # Outbound local activity input (is_local=True)
                ),
                TraceItem(
                    method="from_payload",
                    context=local_activity_context,  # Inbound local activity input (is_local=True)
                ),
                TraceItem(
                    method="to_payload",
                    context=local_activity_context,  # Outbound local activity result (is_local=True)
                ),
                TraceItem(
                    method="from_payload",
                    context=local_activity_context,  # Inbound local activity result (is_local=True)
                ),
                TraceItem(
                    method="to_payload",
                    context=workflow_context,  # Outbound workflow result
                ),
                TraceItem(
                    method="from_payload",
                    context=workflow_context,  # Inbound workflow result
                ),
            ]
        )


# Async activity completion test


@activity.defn
async def async_activity() -> TraceData:
    # Signal that activity has started via heartbeat
    activity.heartbeat("started")
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
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextCompositePayloadConverter,
    )

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
        # Wait a bit for the activity to start
        await asyncio.sleep(0.5)
        data = TraceData()
        await activity_handle.heartbeat(data)
        await activity_handle.complete(data)
        result = await wf_handle.result()

        # project down since activity completion by a client does not have access to most activity
        # context fields
        def project(trace_item: TraceItem) -> str:
            return trace_item.method

        assert [project(item) for item in result.items] == [
            "to_payload",  # Outbound activity input
            "to_payload",  # Outbound activity heartbeat data
            "from_payload",  # Inbound activity result
            "to_payload",  # Outbound workflow result
            "from_payload",  # Inbound workflow result
        ]


# Signal test


@workflow.defn(sandboxed=False)  # so that we can use isinstance
class SignalSerializationContextTestWorkflow:
    def __init__(self) -> None:
        self.signal_received: Optional[TraceData] = None

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
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextCompositePayloadConverter,
    )

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
        assert result.items == [
            TraceItem(
                method="to_payload",
                context=workflow_context,  # Outbound signal input
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context,  # Inbound signal input
            ),
            TraceItem(
                method="to_payload",
                context=workflow_context,  # Outbound workflow result
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context,  # Inbound workflow result
            ),
        ]


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

    config = client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextCompositePayloadConverter,
    )
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
        assert result.items == [
            TraceItem(
                method="to_payload",
                context=workflow_context,  # Outbound query input
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context,  # Inbound query input
            ),
            TraceItem(
                method="to_payload",
                context=workflow_context,  # Outbound query result
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context,  # Inbound query result
            ),
        ]


# Update test


@workflow.defn
class UpdateSerializationContextTestWorkflow:
    @workflow.init
    def __init__(self, pass_validation: bool) -> None:
        self.pass_validation = pass_validation
        self.input: Optional[TraceData] = None

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

    config = client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextCompositePayloadConverter,
    )
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
        assert result.items == [
            TraceItem(
                method="to_payload",
                context=workflow_context,  # Outbound update input
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context,  # Inbound update input
            ),
            TraceItem(
                method="to_payload",
                context=workflow_context,  # Outbound update/workflow result
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context,  # Inbound update/workflow result
            ),
        ]


# External workflow test


@workflow.defn
class ExternalWorkflowTarget:
    def __init__(self) -> None:
        self.signal_received: Optional[TraceData] = None

    @workflow.run
    async def run(self) -> TraceData:
        try:
            await workflow.wait_condition(lambda: self.signal_received is not None)
            return self.signal_received or TraceData()
        except asyncio.CancelledError:
            return TraceData()

    @workflow.signal
    async def external_signal(self, data: TraceData) -> None:
        self.signal_received = data


@workflow.defn
class ExternalWorkflowSignaler:
    @workflow.run
    async def run(self, target_id: str, data: TraceData) -> TraceData:
        handle = workflow.get_external_workflow_handle(target_id)
        await handle.signal(ExternalWorkflowTarget.external_signal, data)
        return data


@workflow.defn
class ExternalWorkflowCanceller:
    @workflow.run
    async def run(self, target_id: str) -> TraceData:
        handle = workflow.get_external_workflow_handle(target_id)
        await handle.cancel()
        return TraceData()


@pytest.mark.timeout(10)
async def test_external_workflow_signal_and_cancel_payload_conversion(
    client: Client,
):
    target_workflow_id = str(uuid.uuid4())
    signaler_workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    config = client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextCompositePayloadConverter,
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[
            ExternalWorkflowTarget,
            ExternalWorkflowSignaler,
            ExternalWorkflowCanceller,
        ],
        activities=[],
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
    ):
        target_handle = await client.start_workflow(
            ExternalWorkflowTarget.run,
            id=target_workflow_id,
            task_queue=task_queue,
        )

        signaler_handle = await client.start_workflow(
            ExternalWorkflowSignaler.run,
            args=[target_workflow_id, TraceData()],
            id=signaler_workflow_id,
            task_queue=task_queue,
        )

        signaler_result = await signaler_handle.result()
        await target_handle.result()

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

        assert (
            signaler_result.items
            == [
                TraceItem(
                    method="to_payload",
                    context=signaler_context,  # Outbound signaler workflow input
                ),
                TraceItem(
                    method="from_payload",
                    context=signaler_context,  # Inbound signaler workflow input
                ),
                TraceItem(
                    method="to_payload",
                    context=target_context,  # Should use target workflow's context for external signal
                ),
                TraceItem(
                    method="to_payload",
                    context=signaler_context,  # Outbound signaler workflow result
                ),
                TraceItem(
                    method="from_payload",
                    context=signaler_context,  # Inbound signaler workflow result
                ),
            ]
        )


# Failure conversion


@activity.defn
async def failing_activity() -> Never:
    raise ApplicationError("test error", dataclasses.asdict(TraceData()))


@workflow.defn
class FailureConverterTestWorkflow:
    @workflow.run
    async def run(self) -> Never:
        await workflow.execute_activity(
            failing_activity,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        raise Exception("Unreachable")


test_traces: dict[str, list[TraceItem]] = defaultdict(list)


class FailureConverterWithContext(DefaultFailureConverter, WithSerializationContext):
    def __init__(self):
        super().__init__(encode_common_attributes=False)
        self.context: Optional[SerializationContext] = None

    def with_context(
        self, context: Optional[SerializationContext]
    ) -> "FailureConverterWithContext":
        converter = FailureConverterWithContext()
        converter.context = context
        return converter

    def to_failure(
        self,
        exception: BaseException,
        payload_converter: PayloadConverter,
        failure: Failure,
    ) -> None:
        assert isinstance(
            self.context, (WorkflowSerializationContext, ActivitySerializationContext)
        )
        test_traces[self.context.workflow_id].append(
            TraceItem(
                method="to_failure",
                context=dataclasses.asdict(self.context),
            )
        )
        super().to_failure(exception, payload_converter, failure)

    def from_failure(
        self, failure: Failure, payload_converter: PayloadConverter
    ) -> BaseException:
        assert isinstance(
            self.context, (WorkflowSerializationContext, ActivitySerializationContext)
        )
        test_traces[self.context.workflow_id].append(
            TraceItem(
                method="from_failure",
                context=dataclasses.asdict(self.context),
            )
        )
        return super().from_failure(failure, payload_converter)


async def test_failure_converter_with_context(client: Client):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    data_converter = dataclasses.replace(
        DataConverter.default,
        failure_converter_class=FailureConverterWithContext,
    )
    config = client.config()
    config["data_converter"] = data_converter
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[FailureConverterTestWorkflow],
        activities=[failing_activity],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        try:
            await client.execute_workflow(
                FailureConverterTestWorkflow.run,
                id=workflow_id,
                task_queue=task_queue,
            )
            raise AssertionError("unreachable")
        except WorkflowFailureError:
            pass

        assert isinstance(data_converter.failure_converter, FailureConverterWithContext)

        workflow_context = dataclasses.asdict(
            WorkflowSerializationContext(
                namespace="default",
                workflow_id=workflow_id,
            )
        )
        activity_context = dataclasses.asdict(
            ActivitySerializationContext(
                namespace="default",
                workflow_id=workflow_id,
                workflow_type=FailureConverterTestWorkflow.__name__,
                activity_type=failing_activity.__name__,
                activity_task_queue=task_queue,
                is_local=False,
            )
        )
        assert test_traces[workflow_id] == (
            [
                TraceItem(
                    context=activity_context,
                    method="to_failure",  # outbound activity result
                )
            ]
            + (
                [
                    TraceItem(
                        context=activity_context,
                        method="from_failure",  # inbound activity result
                    )
                ]
                * 2  # from_failure deserializes the error and error cause
            )
            + [
                TraceItem(
                    context=workflow_context,
                    method="to_failure",  # outbound workflow result
                )
            ]
            + (
                [
                    TraceItem(
                        context=workflow_context,
                        method="from_failure",  # inbound workflow result
                    )
                ]
                * 2  # from_failure deserializes the error and error cause
            )
        )
        del test_traces[workflow_id]


class PayloadCodecWithContext(PayloadCodec, WithSerializationContext):
    def __init__(self):
        self.context: Optional[SerializationContext] = None
        self.encode_called_with_context = False
        self.decode_called_with_context = False

    def with_context(
        self, context: Optional[SerializationContext]
    ) -> "PayloadCodecWithContext":
        codec = PayloadCodecWithContext()
        codec.context = context
        return codec

    async def encode(self, payloads: Sequence[Payload]) -> List[Payload]:
        assert self.context
        if isinstance(self.context, ActivitySerializationContext):
            test_traces[self.context.workflow_id].append(
                TraceItem(
                    context=dataclasses.asdict(self.context),
                    method="encode",
                )
            )
        else:
            assert isinstance(self.context, WorkflowSerializationContext)
            test_traces[self.context.workflow_id].append(
                TraceItem(
                    context=dataclasses.asdict(self.context),
                    method="encode",
                )
            )
        return list(payloads)

    async def decode(self, payloads: Sequence[Payload]) -> List[Payload]:
        assert self.context
        if isinstance(self.context, ActivitySerializationContext):
            test_traces[self.context.workflow_id].append(
                TraceItem(
                    context=dataclasses.asdict(self.context),
                    method="decode",
                )
            )
        else:
            assert isinstance(self.context, WorkflowSerializationContext)
            test_traces[self.context.workflow_id].append(
                TraceItem(
                    context=dataclasses.asdict(self.context),
                    method="decode",
                )
            )
        return list(payloads)


@workflow.defn
class CodecTestWorkflow:
    @workflow.run
    async def run(self, data: str) -> str:
        return data


async def test_codec_with_context(client: Client):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    client_config = client.config()
    client_config["data_converter"] = dataclasses.replace(
        DataConverter.default, payload_codec=PayloadCodecWithContext()
    )
    client = Client(**client_config)
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[CodecTestWorkflow],
    ):
        await client.execute_workflow(
            CodecTestWorkflow.run,
            "data",
            id=workflow_id,
            task_queue=task_queue,
        )
    workflow_context = dataclasses.asdict(
        WorkflowSerializationContext(
            namespace=client.namespace,
            workflow_id=workflow_id,
        )
    )
    assert test_traces[workflow_id] == [
        TraceItem(
            context=workflow_context,
            method="encode",
        ),
        TraceItem(
            context=workflow_context,
            method="decode",
        ),
        TraceItem(
            context=workflow_context,
            method="encode",
        ),
        TraceItem(
            context=workflow_context,
            method="decode",
        ),
    ]
    del test_traces[workflow_id]


@activity.defn
async def codec_test_local_activity(data: str) -> str:
    return data


@workflow.defn
class LocalActivityCodecTestWorkflow:
    @workflow.run
    async def run(self, data: str) -> str:
        return await workflow.execute_local_activity(
            codec_test_local_activity,
            data,
            start_to_close_timeout=timedelta(seconds=10),
        )


async def test_local_activity_codec_with_context(client: Client):
    """Test that codec gets correct context with is_local=True for local activities."""
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    client_config = client.config()
    client_config["data_converter"] = dataclasses.replace(
        DataConverter.default, payload_codec=PayloadCodecWithContext()
    )
    client = Client(**client_config)
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[LocalActivityCodecTestWorkflow],
        activities=[codec_test_local_activity],
    ):
        await client.execute_workflow(
            LocalActivityCodecTestWorkflow.run,
            "data",
            id=workflow_id,
            task_queue=task_queue,
        )

    workflow_context = dataclasses.asdict(
        WorkflowSerializationContext(
            namespace=client.namespace,
            workflow_id=workflow_id,
        )
    )
    local_activity_context = dataclasses.asdict(
        ActivitySerializationContext(
            namespace=client.namespace,
            workflow_id=workflow_id,
            workflow_type=LocalActivityCodecTestWorkflow.__name__,
            activity_type=codec_test_local_activity.__name__,
            activity_task_queue=task_queue,
            is_local=True,  # Should be True for local activities
        )
    )

    # Note: Local activities have partial activity context support through codec
    # The input encode uses workflow context, but the decode uses activity context
    # The result encode uses activity context, but the decode uses workflow context
    assert test_traces[workflow_id] == [
        # Workflow input
        TraceItem(
            context=workflow_context,
            method="encode",
        ),
        TraceItem(
            context=workflow_context,
            method="decode",
        ),
        # Local activity input - encode uses workflow context
        TraceItem(
            context=workflow_context,
            method="encode",
        ),
        # Local activity input - decode uses activity context with is_local=True
        TraceItem(
            context=local_activity_context,
            method="decode",
        ),
        # Local activity result - encode uses activity context with is_local=True
        TraceItem(
            context=local_activity_context,
            method="encode",
        ),
        # Local activity result - decode uses workflow context
        TraceItem(
            context=workflow_context,
            method="decode",
        ),
        # Workflow result
        TraceItem(
            context=workflow_context,
            method="encode",
        ),
        TraceItem(
            context=workflow_context,
            method="decode",
        ),
    ]
    del test_traces[workflow_id]


# Pydantic


class PydanticData(BaseModel):
    value: str
    trace: List[str] = []


class PydanticJSONConverterWithContext(
    PydanticJSONPlainPayloadConverter, WithSerializationContext
):
    def __init__(self):
        super().__init__()
        self.context: Optional[SerializationContext] = None

    def with_context(
        self, context: Optional[SerializationContext]
    ) -> "PydanticJSONConverterWithContext":
        converter = PydanticJSONConverterWithContext()
        converter.context = context
        return converter

    def to_payload(self, value: Any) -> Optional[Payload]:
        if isinstance(value, PydanticData) and self.context:
            if isinstance(self.context, WorkflowSerializationContext):
                value.trace.append(f"wf_{self.context.workflow_id}")
        return super().to_payload(value)


class PydanticConverterWithContext(CompositePayloadConverter, WithSerializationContext):
    def __init__(self):
        super().__init__(
            *(
                c
                if not isinstance(c, JSONPlainPayloadConverter)
                else PydanticJSONConverterWithContext()
                for c in DefaultPayloadConverter.default_encoding_payload_converters
            )
        )
        self.context: Optional[SerializationContext] = None


@workflow.defn
class PydanticContextWorkflow:
    @workflow.run
    async def run(self, data: PydanticData) -> PydanticData:
        return data


async def test_pydantic_converter_with_context(client: Client):
    wf_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    client_config = client.config()
    client_config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=PydanticConverterWithContext,
    )
    client = Client(**client_config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[PydanticContextWorkflow],
    ):
        result = await client.execute_workflow(
            PydanticContextWorkflow.run,
            PydanticData(value="test"),
            id=wf_id,
            task_queue=task_queue,
        )
        assert f"wf_{wf_id}" in result.trace
