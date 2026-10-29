"""
Test context-aware serde/codec operations.

Serialization context should be available on all serde/codec operations, but testing all of them is
infeasible; this test suite only covers a selection.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

import nexusrpc
import pytest
from pydantic import BaseModel
from typing_extensions import Never

import temporalio.api.common.v1
import temporalio.api.failure.v1
import temporalio.api.history.v1
import temporalio.api.nexus.v1
import temporalio.api.update.v1
import temporalio.api.workflowservice.v1
from temporalio import activity, nexus, workflow
from temporalio.client import (
    ActivityFailureError,
    AsyncActivityHandle,
    Client,
    GetNexusOperationResultInput,
    Interceptor,
    NexusOperationFailureError,
    OutboundInterceptor,
    StartWorkflowUpdateInput,
    WorkflowFailureError,
    WorkflowUpdateFailedError,
    WorkflowUpdateHandle,
    WorkflowUpdateStage,
)
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
    NexusSerializationContext,
    PayloadCodec,
    PayloadConverter,
    SerializationContext,
    WithSerializationContext,
    WorkflowSerializationContext,
)
from temporalio.exceptions import ApplicationError, NexusOperationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from temporalio.worker._workflow_instance import UnsandboxedWorkflowRunner
from tests.helpers import assert_eq_eventually
from tests.helpers.nexus import make_nexus_endpoint_name


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
        self.context: SerializationContext | None = None

    @property
    def encoding(self) -> str:
        return "test-serialization-context"

    def with_context(
        self, context: SerializationContext | None
    ) -> SerializationContextPayloadConverter:
        converter = SerializationContextPayloadConverter()
        converter.context = context
        return converter

    def to_payload(self, value: Any) -> temporalio.api.common.v1.Payload | None:
        if not isinstance(value, TraceData):
            return None
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

    def from_payload(
        self,
        payload: temporalio.api.common.v1.Payload,
        type_hint: type | None = None,
    ) -> Any:
        value = JSONPlainPayloadConverter().from_payload(payload, TraceData)
        assert isinstance(value, TraceData)
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


# Payload conversion tests

## Misc payload conversion


@activity.defn
async def passthrough_activity(input: TraceData) -> TraceData:
    activity.payload_converter().to_payload(input)
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
        workflow.payload_converter().to_payload(data)
        data = await workflow.execute_activity(
            passthrough_activity,
            data,
            start_to_close_timeout=timedelta(seconds=10),
            heartbeat_timeout=timedelta(seconds=2),
            activity_id="activity-id",
        )
        data = await workflow.execute_child_workflow(
            EchoWorkflow.run, data, id=f"{workflow.info().workflow_id}_child"
        )
        return data


async def test_payload_conversion_calls_follow_expected_sequence_and_contexts(
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
                namespace=client.namespace,
                workflow_id=workflow_id,
            )
        )
        child_workflow_context = dataclasses.asdict(
            WorkflowSerializationContext(
                namespace=client.namespace,
                workflow_id=f"{workflow_id}_child",
            )
        )
        activity_context = dataclasses.asdict(
            ActivitySerializationContext(
                namespace=client.namespace,
                workflow_id=workflow_id,
                workflow_type=PayloadConversionWorkflow.__name__,
                activity_type=passthrough_activity.__name__,
                activity_id="activity-id",
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
                context=workflow_context,  # workflow payload converter
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
                context=activity_context,  # activity payload converter
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


## Activity heartbeat payload conversion


@activity.defn
async def activity_with_heartbeat_details() -> TraceData:
    info = activity.info()
    if info.attempt == 1:
        data = TraceData()
        activity.heartbeat(data)
        raise Exception("Intentional error to force retry")
    elif info.attempt == 2:
        [heartbeat_data] = info.heartbeat_details
        assert isinstance(heartbeat_data, TraceData)
        return heartbeat_data
    else:
        raise AssertionError(f"Unexpected attempt number: {info.attempt}")


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
            activity_id="activity-id",
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

        workflow_context = dataclasses.asdict(
            WorkflowSerializationContext(
                namespace=client.namespace,
                workflow_id=workflow_id,
            )
        )

        activity_context = dataclasses.asdict(
            ActivitySerializationContext(
                namespace=client.namespace,
                workflow_id=workflow_id,
                workflow_type=HeartbeatDetailsSerializationContextTestWorkflow.__name__,
                activity_type=activity_with_heartbeat_details.__name__,
                activity_id="activity-id",
                activity_task_queue=task_queue,
                is_local=False,
            )
        )

        assert result.items == [
            TraceItem(
                method="to_payload",
                context=activity_context,  # Outbound heartbeat
            ),
            TraceItem(
                method="from_payload",
                context=activity_context,  # Inbound heartbeart detail
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
                context=workflow_context,  # Outbound workflow result
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context,  # Inbound workflow result
            ),
        ]


## Local activity payload conversion


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
            activity_id="activity-id",
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
                namespace=client.namespace,
                workflow_id=workflow_id,
            )
        )
        local_activity_context = dataclasses.asdict(
            ActivitySerializationContext(
                namespace=client.namespace,
                workflow_id=workflow_id,
                workflow_type=LocalActivityWorkflow.__name__,
                activity_type=local_activity.__name__,
                activity_id="activity-id",
                activity_task_queue=task_queue,
                is_local=True,
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
                context=local_activity_context,  # Outbound local activity input
            ),
            TraceItem(
                method="from_payload",
                context=local_activity_context,  # Inbound local activity input
            ),
            TraceItem(
                method="to_payload",
                context=local_activity_context,  # Outbound local activity result
            ),
            TraceItem(
                method="from_payload",
                context=local_activity_context,  # Inbound local activity result
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


## Async activity completion payload conversion


@workflow.defn
class WaitForSignalWorkflow:
    # Like a global asyncio.Event()

    def __init__(self) -> None:
        self.signal_received = asyncio.Event()

    @workflow.run
    async def run(self) -> None:
        await self.signal_received.wait()

    @workflow.signal
    def signal(self) -> None:
        self.signal_received.set()


@activity.defn
async def async_activity() -> TraceData:
    # Notify test that the activity has started and is ready to be completed manually
    await (
        activity.client()
        .get_workflow_handle("activity-started-wf-id")
        .signal(WaitForSignalWorkflow.signal)
    )
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
        workflows=[
            AsyncActivityCompletionSerializationContextTestWorkflow,
            WaitForSignalWorkflow,
        ],
        activities=[async_activity],
        workflow_runner=UnsandboxedWorkflowRunner(),  # so that we can use isinstance
    ):
        workflow_context = WorkflowSerializationContext(
            namespace=client.namespace,
            workflow_id=workflow_id,
        )
        activity_context = ActivitySerializationContext(
            namespace=client.namespace,
            workflow_id=workflow_id,
            workflow_type=AsyncActivityCompletionSerializationContextTestWorkflow.__name__,
            activity_type=async_activity.__name__,
            activity_id="async-activity-id",
            activity_task_queue=task_queue,
            is_local=False,
        )

        act_started_wf_handle = await client.start_workflow(
            WaitForSignalWorkflow.run,
            id="activity-started-wf-id",
            task_queue=task_queue,
        )
        wf_handle = await client.start_workflow(
            AsyncActivityCompletionSerializationContextTestWorkflow.run,
            id=workflow_id,
            task_queue=task_queue,
        )
        activity_handle = client.get_async_activity_handle(
            workflow_id=workflow_id,
            run_id=wf_handle.first_execution_run_id,
            activity_id="async-activity-id",
        ).with_context(activity_context)

        await act_started_wf_handle.result()
        data = TraceData()
        await activity_handle.heartbeat(data)
        await activity_handle.complete(data)
        result = await wf_handle.result()

        activity_context_dict = dataclasses.asdict(activity_context)
        workflow_context_dict = dataclasses.asdict(workflow_context)

        assert result.items == [
            TraceItem(
                method="to_payload",
                context=activity_context_dict,  # Outbound activity heartbeat
            ),
            TraceItem(
                method="to_payload",
                context=activity_context_dict,  # Outbound activity completion
            ),
            TraceItem(
                method="from_payload",
                context=activity_context_dict,  # Inbound activity result
            ),
            TraceItem(
                method="to_payload",
                context=workflow_context_dict,  # Outbound workflow result
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context_dict,  # Inbound workflow result
            ),
        ]


class MyAsyncActivityHandle(AsyncActivityHandle):
    def my_method(self) -> None:
        pass


class MyAsyncActivityHandleWithOverriddenConstructor(AsyncActivityHandle):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def my_method(self) -> None:
        pass


def test_subclassed_async_activity_handle(client: Client):
    activity_context = ActivitySerializationContext(
        namespace=client.namespace,
        workflow_id="workflow-id",
        workflow_type="workflow-type",
        activity_type="activity-type",
        activity_id="activity-id",
        activity_task_queue="activity-task-queue",
        is_local=False,
    )
    handle = MyAsyncActivityHandle(client=client, id_or_token=b"task-token")
    # This works because the data converter does not use context so AsyncActivityHandle.with_context
    # returns self
    assert isinstance(handle.with_context(activity_context), MyAsyncActivityHandle)

    # This time the data converter uses context so AsyncActivityHandle.with_context attempts to
    # return a new instance of the user's subclass. It works, because they have not overridden the
    # constructor.
    client_config = client.config()
    client_config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=SerializationContextCompositePayloadConverter,
    )
    client = Client(**client_config)
    handle = MyAsyncActivityHandle(client=client, id_or_token=b"task-token")
    assert isinstance(handle.with_context(activity_context), MyAsyncActivityHandle)

    # Finally, a user attempts the same but having overridden the constructor. This fails:
    # AsyncActivityHandle.with_context refuses to attempt to create an instance of their subclass.
    handle2 = MyAsyncActivityHandleWithOverriddenConstructor(
        client=client, id_or_token=b"task-token"
    )
    with pytest.raises(
        TypeError,
        match="you must override with_context to return an instance of your class",
    ):
        assert isinstance(
            handle2.with_context(activity_context),
            MyAsyncActivityHandleWithOverriddenConstructor,
        )


# Signal test


@workflow.defn(sandboxed=False)  # so that we can use isinstance
class SignalSerializationContextTestWorkflow:
    def __init__(self) -> None:
        self.signal_received: TraceData | None = None

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
                namespace=client.namespace,
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
                namespace=client.namespace,
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
        self.input: TraceData | None = None

    @workflow.run
    async def run(self, _pass_validation: bool) -> TraceData:
        await workflow.wait_condition(lambda: self.input is not None)
        assert self.input
        return self.input

    @workflow.update
    def my_update(self, input: TraceData) -> TraceData:
        self.input = input
        return input

    @my_update.validator
    def my_update_validator(self, input: TraceData) -> None:
        if not self.pass_validation:
            raise ApplicationError("Rejected", input)


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
            except WorkflowUpdateFailedError as e:
                assert isinstance(e.cause, ApplicationError)
                assert len(e.cause.details) == 1
                result = e.cause.details[0]
                assert isinstance(result, TraceData)
            await wf_handle.terminate()

        workflow_context = dataclasses.asdict(
            WorkflowSerializationContext(
                namespace=client.namespace,
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
                context=workflow_context,  # Outbound update result or error detail
            ),
            TraceItem(
                method="from_payload",
                context=workflow_context,  # Inbound update result or error detail
            ),
        ]


# External workflow test


@workflow.defn
class ExternalWorkflowTarget:
    def __init__(self) -> None:
        self.signal_received: TraceData | None = None

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
                namespace=client.namespace,
                workflow_id=signaler_workflow_id,
            )
        )
        target_context = dataclasses.asdict(
            WorkflowSerializationContext(
                namespace=client.namespace,
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
            activity_id="activity-id",
        )
        raise Exception("Unreachable")


test_traces: dict[str | None, list[TraceItem]] = defaultdict(list)


class FailureConverterWithContext(DefaultFailureConverter, WithSerializationContext):
    def __init__(self):
        super().__init__(encode_common_attributes=False)
        self.context: SerializationContext | None = None

    def with_context(
        self, context: SerializationContext | None
    ) -> FailureConverterWithContext:
        converter = FailureConverterWithContext()
        converter.context = context
        return converter

    def to_failure(
        self,
        exception: BaseException,
        payload_converter: PayloadConverter,
        failure: temporalio.api.failure.v1.Failure,
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
        self,
        failure: temporalio.api.failure.v1.Failure,
        payload_converter: PayloadConverter,
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
                namespace=client.namespace,
                workflow_id=workflow_id,
            )
        )
        activity_context = dataclasses.asdict(
            ActivitySerializationContext(
                namespace=client.namespace,
                workflow_id=workflow_id,
                workflow_type=FailureConverterTestWorkflow.__name__,
                activity_type=failing_activity.__name__,
                activity_id="activity-id",
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


# Test payload codec


class PayloadCodecWithContext(PayloadCodec, WithSerializationContext):
    def __init__(self):
        self.context: SerializationContext | None = None
        self.encode_called_with_context = False
        self.decode_called_with_context = False

    def with_context(
        self, context: SerializationContext | None
    ) -> PayloadCodecWithContext:
        codec = PayloadCodecWithContext()
        codec.context = context
        return codec

    async def encode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
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

    async def decode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
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


# Local activity codec test


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
            activity_id="activity-id",
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
            activity_id="activity-id",
            activity_task_queue=task_queue,
            is_local=True,
        )
    )

    assert test_traces[workflow_id] == [
        TraceItem(
            context=workflow_context,
            method="encode",  # outbound workflow input
        ),
        TraceItem(
            context=workflow_context,
            method="decode",  # inbound workflow input
        ),
        TraceItem(
            context=local_activity_context,
            method="encode",  # outbound local activity input
        ),
        TraceItem(
            context=local_activity_context,
            method="decode",  # inbound local activity input
        ),
        TraceItem(
            context=local_activity_context,
            method="encode",  # outbound local activity result
        ),
        TraceItem(
            context=local_activity_context,
            method="decode",  # inbound local activity result
        ),
        TraceItem(
            context=workflow_context,
            method="encode",  # outbound workflow result
        ),
        TraceItem(
            context=workflow_context,
            method="decode",  # inbound workflow result
        ),
    ]
    del test_traces[workflow_id]


# Child workflow codec test


@workflow.defn
class ChildWorkflowCodecTestWorkflow:
    @workflow.run
    async def run(self, data: TraceData) -> TraceData:
        return await workflow.execute_child_workflow(
            EchoWorkflow.run,
            data,
            id=f"{workflow.info().workflow_id}-child",
        )


async def test_child_workflow_codec_with_context(client: Client):
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    child_workflow_id = f"{workflow_id}-child"

    config = client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_codec=PayloadCodecWithContext(),
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ChildWorkflowCodecTestWorkflow, EchoWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await client.execute_workflow(
            ChildWorkflowCodecTestWorkflow.run,
            TraceData(),
            id=workflow_id,
            task_queue=task_queue,
        )

    parent_workflow_context = dataclasses.asdict(
        WorkflowSerializationContext(
            namespace=client.namespace,
            workflow_id=workflow_id,
        )
    )
    child_workflow_context = dataclasses.asdict(
        WorkflowSerializationContext(
            namespace=client.namespace,
            workflow_id=child_workflow_id,
        )
    )

    assert test_traces[workflow_id] == [
        TraceItem(
            context=parent_workflow_context,
            method="encode",  # outbound workflow input
        ),
        TraceItem(
            context=parent_workflow_context,
            method="decode",  # inbound workflow input
        ),
        TraceItem(
            context=parent_workflow_context,
            method="encode",  # outbound workflow result
        ),
        TraceItem(
            context=parent_workflow_context,
            method="decode",  # inbound workflow result
        ),
    ]
    assert test_traces[child_workflow_id] == [
        TraceItem(
            context=child_workflow_context,
            method="encode",  # outbound child workflow input
        ),
        TraceItem(
            context=child_workflow_context,
            method="decode",  # inbound child workflow input
        ),
        TraceItem(
            context=child_workflow_context,
            method="encode",  # outbound child workflow result
        ),
        TraceItem(
            context=child_workflow_context,
            method="decode",  # inbound child workflow result
        ),
    ]
    del test_traces[workflow_id]
    del test_traces[child_workflow_id]


# Payload codec: test decode context matches encode context


class PayloadEncryptionCodec(PayloadCodec, WithSerializationContext):
    """
    The outbound data for encoding must always be the string "outbound". "Encrypt" it by replacing
    it with a key that is derived from the context available during encoding. On decryption, assert
    that the same key can be derived from the context available during decoding, and return the
    string "inbound".
    """

    def __init__(self):
        self.context: SerializationContext | None = None

    def with_context(
        self, context: SerializationContext | None
    ) -> PayloadEncryptionCodec:
        codec = PayloadEncryptionCodec()
        codec.context = context
        return codec

    async def encode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        [payload] = payloads
        return [
            temporalio.api.common.v1.Payload(
                metadata=payload.metadata,
                data=json.dumps(self._get_encryption_key()).encode(),
            )
        ]

    async def decode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        [payload] = payloads
        assert json.loads(payload.data.decode()) == self._get_encryption_key()
        metadata = dict(payload.metadata)
        return [temporalio.api.common.v1.Payload(metadata=metadata, data=b'"inbound"')]

    def _get_encryption_key(self) -> str:
        context = (
            dataclasses.asdict(self.context)
            if isinstance(
                self.context,
                (WorkflowSerializationContext, ActivitySerializationContext),
            )
            else {}
        )
        return json.dumps({k: v for k, v in sorted(context.items())})


@activity.defn
async def payload_encryption_activity(data: str) -> str:
    assert data == "inbound"
    return "outbound"


@workflow.defn
class PayloadEncryptionChildWorkflow:
    @workflow.run
    async def run(self, data: str) -> str:
        assert data == "inbound"
        return "outbound"


@nexusrpc.service
class PayloadEncryptionService:
    payload_encryption_operation: nexusrpc.Operation[str, str]


@nexusrpc.handler.service_handler
class PayloadEncryptionServiceHandler:
    @nexusrpc.handler.sync_operation
    async def payload_encryption_operation(
        self, _: nexusrpc.handler.StartOperationContext, data: str
    ) -> str:
        assert data == "inbound"
        return "outbound"


@workflow.defn
class PayloadEncryptionWorkflow:
    def __init__(self):
        self.received_signal = False
        self.received_update = False

    @workflow.run
    async def run(self, _data: str) -> str:
        await workflow.wait_condition(
            lambda: self.received_signal and self.received_update
        )
        # Run them in parallel to check that data converter operations do not mix up contexts when
        # there are multiple concurrent payload types.
        coros = [
            workflow.execute_activity(
                payload_encryption_activity,
                "outbound",
                start_to_close_timeout=timedelta(seconds=10),
                activity_id="activity-id",
            ),
            workflow.execute_child_workflow(
                PayloadEncryptionChildWorkflow.run,
                "outbound",
                id=f"{workflow.info().workflow_id}_child",
            ),
        ]
        [act_result, cw_result], _ = await workflow.wait(
            [asyncio.create_task(c) for c in coros]
        )
        assert await act_result == "inbound"
        assert await cw_result == "inbound"
        return "outbound"

    @workflow.query
    def query(self, data: str) -> str:
        assert data == "inbound"
        return "outbound"

    @workflow.signal
    def signal(self, data: str) -> None:
        assert data == "inbound"
        self.received_signal = True

    @workflow.update
    def update(self, data: str) -> str:
        assert data == "inbound"
        self.received_update = True
        return "outbound"

    @update.validator
    def update_validator(self, data: str) -> None:
        assert data == "inbound"


async def test_decode_context_matches_encode_context(
    client: Client,
):
    """
    Encode outbound payloads with a key using all available context fields, in order to demonstrate
    that the same context is available to decode inbound payloads.
    """
    workflow_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    config = client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_codec=PayloadEncryptionCodec(),
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[PayloadEncryptionWorkflow, PayloadEncryptionChildWorkflow],
        activities=[payload_encryption_activity],
        nexus_service_handlers=[PayloadEncryptionServiceHandler()],
    ):
        wf_handle = await client.start_workflow(
            PayloadEncryptionWorkflow.run,
            "outbound",
            id=workflow_id,
            task_queue=task_queue,
        )
        assert "inbound" == await wf_handle.query(
            PayloadEncryptionWorkflow.query, "outbound"
        )
        await wf_handle.signal(PayloadEncryptionWorkflow.signal, "outbound")
        assert "inbound" == await wf_handle.execute_update(
            PayloadEncryptionWorkflow.update, "outbound"
        )
        assert "inbound" == await wf_handle.result()


# Test nexus payload codec


class _NexusResultDecodingInterceptor(Interceptor):
    def __init__(self) -> None:
        super().__init__()
        self.decoded_results: list[Any] = []

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return _NexusResultDecodingOutboundInterceptor(next, self)


class _NexusResultDecodingOutboundInterceptor(OutboundInterceptor):
    def __init__(
        self, next: OutboundInterceptor, parent: _NexusResultDecodingInterceptor
    ) -> None:
        super().__init__(next)
        self._parent = parent

    async def get_nexus_operation_result(
        self, input: GetNexusOperationResultInput
    ) -> Any:
        result = await super().get_nexus_operation_result(input)
        self._parent.decoded_results.append(result)
        return result


class NexusContextMarkerPayloadCodec(PayloadCodec, WithSerializationContext):
    MARKER_KEY = "nexus-context-marker"

    def __init__(
        self,
        markers: dict[NexusSerializationContext, bytes],
        context: SerializationContext | None = None,
    ):
        self.markers = markers
        self.context = context

    def with_context(
        self, context: SerializationContext
    ) -> NexusContextMarkerPayloadCodec:
        return NexusContextMarkerPayloadCodec(self.markers, context)

    def _marker(self) -> bytes | None:
        if not isinstance(self.context, NexusSerializationContext):
            return None
        try:
            return self.markers[self.context]
        except KeyError:
            raise AssertionError(
                f"No Nexus payload codec configured for {self.context!r}"
            ) from None

    async def encode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        marker = self._marker()
        if marker is None:
            return list(payloads)
        encoded = []
        for payload in payloads:
            marked_payload = temporalio.api.common.v1.Payload()
            marked_payload.CopyFrom(payload)
            marked_payload.metadata[self.MARKER_KEY] = marker
            encoded.append(marked_payload)
        return encoded

    async def decode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        marker = self._marker()
        if marker is None:
            # Every reader must decode under the same Nexus context the payload was encoded with
            for payload in payloads:
                assert self.MARKER_KEY not in payload.metadata, (
                    f"payload encoded under a Nexus context was decoded under "
                    f"{self.context!r}"
                )
            return list(payloads)
        decoded = []
        for payload in payloads:
            actual_marker = payload.metadata.get(self.MARKER_KEY)
            if actual_marker is None:
                decoded.append(payload)
                continue
            assert actual_marker == marker
            decoded_payload = temporalio.api.common.v1.Payload()
            decoded_payload.CopyFrom(payload)
            del decoded_payload.metadata[self.MARKER_KEY]
            decoded.append(decoded_payload)
        return decoded


@nexusrpc.handler.service_handler
class NexusOperationTestServiceHandler:
    @nexusrpc.handler.sync_operation
    async def operation(
        self, _: nexusrpc.handler.StartOperationContext, data: str
    ) -> str:
        return data

    @nexusrpc.handler.sync_operation
    async def fail(self, _: nexusrpc.handler.StartOperationContext, data: str) -> str:
        raise ApplicationError(data, non_retryable=True)


@workflow.defn
class NexusOperationTestWorkflow:
    @workflow.run
    async def run(self, red_endpoint_name: str, blue_endpoint_name: str) -> list[str]:
        red_handle, blue_handle = await asyncio.gather(
            workflow.create_nexus_client(
                service=NexusOperationTestServiceHandler,
                endpoint=red_endpoint_name,
            ).start_operation(
                NexusOperationTestServiceHandler.operation,
                input="nexus-data",
                summary="nexus-summary",
            ),
            workflow.create_nexus_client(
                service=NexusOperationTestServiceHandler,
                endpoint=blue_endpoint_name,
            ).start_operation(
                NexusOperationTestServiceHandler.operation,
                input="nexus-data",
                summary="nexus-summary",
            ),
        )
        return list(await asyncio.gather(red_handle, blue_handle))


@workflow.defn
class NexusOperationFailureTestWorkflow:
    @workflow.run
    async def run(self, endpoint_name: str) -> None:
        nexus_client = workflow.create_nexus_client(
            service=NexusOperationTestServiceHandler,
            endpoint=endpoint_name,
        )
        try:
            await nexus_client.start_operation(
                NexusOperationTestServiceHandler.fail, input="nexus-failure"
            )
        except NexusOperationError:
            return
        raise AssertionError("Nexus operation should have failed")


nexus_failure_context_traces: list[tuple[str, NexusSerializationContext]] = []


class NexusFailureConverterWithContext(
    DefaultFailureConverter, WithSerializationContext
):
    def __init__(self, context: SerializationContext | None = None):
        super().__init__()
        self.context = context

    def with_context(
        self, context: SerializationContext
    ) -> NexusFailureConverterWithContext:
        return NexusFailureConverterWithContext(context)

    def to_failure(
        self,
        exception: BaseException,
        payload_converter: PayloadConverter,
        failure: temporalio.api.failure.v1.Failure,
    ) -> None:
        if isinstance(self.context, NexusSerializationContext):
            nexus_failure_context_traces.append(("to_failure", self.context))
        super().to_failure(exception, payload_converter, failure)

    def from_failure(
        self,
        failure: temporalio.api.failure.v1.Failure,
        payload_converter: PayloadConverter,
    ) -> BaseException:
        if isinstance(self.context, NexusSerializationContext):
            nexus_failure_context_traces.append(("from_failure", self.context))
        return super().from_failure(failure, payload_converter)


@pytest.mark.requires_local_server
async def test_workflow_nexus_payload_codec_receives_context(
    env: WorkflowEnvironment,
):
    """Nexus payload codecs get context for workflow inputs, summaries, and results."""
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    task_queue = "workflow-nexus-context-codec-task-queue"
    red_endpoint_name = "workflow-red-nexus-endpoint"
    blue_endpoint_name = "workflow-blue-nexus-endpoint"
    red_context = NexusSerializationContext(
        endpoint=red_endpoint_name,
        service="NexusOperationTestServiceHandler",
        operation="operation",
    )
    blue_context = NexusSerializationContext(
        endpoint=blue_endpoint_name,
        service="NexusOperationTestServiceHandler",
        operation="operation",
    )
    payload_codec = NexusContextMarkerPayloadCodec(
        {red_context: b"red", blue_context: b"blue"}
    )
    config = env.client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_codec=payload_codec,
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[NexusOperationTestWorkflow],
        nexus_service_handlers=[NexusOperationTestServiceHandler()],
    ) as worker:
        await env.create_nexus_endpoint(red_endpoint_name, worker.task_queue)
        await env.create_nexus_endpoint(blue_endpoint_name, worker.task_queue)
        handle = await client.start_workflow(
            NexusOperationTestWorkflow.run,
            args=[red_endpoint_name, blue_endpoint_name],
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        assert await handle.result() == ["nexus-data", "nexus-data"]

        history = await handle.fetch_history()
        scheduled_endpoints: dict[int, str] = {}
        encoded_summaries: dict[str, temporalio.api.common.v1.Payload] = {}
        encoded_results: dict[str, temporalio.api.common.v1.Payload] = {}
        for event in history.events:
            if event.HasField("nexus_operation_scheduled_event_attributes"):
                scheduled_attrs = event.nexus_operation_scheduled_event_attributes
                assert scheduled_attrs.service == "NexusOperationTestServiceHandler"
                assert scheduled_attrs.operation == "operation"
                scheduled_endpoints[event.event_id] = scheduled_attrs.endpoint
                assert event.HasField("user_metadata")
                assert event.user_metadata.HasField("summary")
                encoded_summaries[scheduled_attrs.endpoint] = (
                    event.user_metadata.summary
                )
            elif event.HasField("nexus_operation_completed_event_attributes"):
                completed_attrs = event.nexus_operation_completed_event_attributes
                endpoint = scheduled_endpoints[completed_attrs.scheduled_event_id]
                encoded_results[endpoint] = completed_attrs.result
        assert set(scheduled_endpoints.values()) == {
            red_endpoint_name,
            blue_endpoint_name,
        }
        assert {
            endpoint: payload.metadata[NexusContextMarkerPayloadCodec.MARKER_KEY]
            for endpoint, payload in encoded_summaries.items()
        } == {
            red_endpoint_name: b"red",
            blue_endpoint_name: b"blue",
        }
        assert {
            endpoint: payload.metadata[NexusContextMarkerPayloadCodec.MARKER_KEY]
            for endpoint, payload in encoded_results.items()
        } == {
            red_endpoint_name: b"red",
            blue_endpoint_name: b"blue",
        }

        scheduled_contexts: dict[int, NexusSerializationContext] = {}
        for event in history.events:
            if event.HasField("nexus_operation_scheduled_event_attributes"):
                scheduled_attrs = event.nexus_operation_scheduled_event_attributes
                context = NexusSerializationContext(
                    endpoint=scheduled_attrs.endpoint,
                    service=scheduled_attrs.service,
                    operation=scheduled_attrs.operation,
                )
                scheduled_contexts[event.event_id] = context
                [decoded] = await payload_codec.with_context(context).decode(
                    [scheduled_attrs.input]
                )
                scheduled_attrs.input.CopyFrom(decoded)
                [decoded] = await payload_codec.with_context(context).decode(
                    [event.user_metadata.summary]
                )
                event.user_metadata.summary.CopyFrom(decoded)
            elif event.HasField("nexus_operation_completed_event_attributes"):
                completed_attrs = event.nexus_operation_completed_event_attributes
                context = scheduled_contexts[completed_attrs.scheduled_event_id]
                [decoded] = await payload_codec.with_context(context).decode(
                    [completed_attrs.result]
                )
                completed_attrs.result.CopyFrom(decoded)
        await Replayer(
            workflows=[NexusOperationTestWorkflow],
            data_converter=config["data_converter"],
        ).replay_workflow(history)


@pytest.mark.requires_local_server
async def test_standalone_nexus_handle_obtained_by_id_uses_server_context(
    env: WorkflowEnvironment,
):
    """A handle obtained by operation ID alone decodes its result with the context the server
    returns, since it never issued a start request to build a context from.
    """
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    task_queue = "standalone-nexus-detached-handle-task-queue"
    endpoint_name = f"standalone-detached-nexus-endpoint-{uuid.uuid4()}"
    context = NexusSerializationContext(
        endpoint=endpoint_name,
        service="NexusOperationTestServiceHandler",
        operation="operation",
    )
    config = env.client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_codec=NexusContextMarkerPayloadCodec({context: b"detached"}),
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[NexusOperationTestServiceHandler()],
    ) as worker:
        await env.create_nexus_endpoint(endpoint_name, worker.task_queue)
        operation_id = str(uuid.uuid4())
        started = await client.create_nexus_client(
            service=NexusOperationTestServiceHandler,
            endpoint=endpoint_name,
        ).start_operation(
            NexusOperationTestServiceHandler.operation,
            "detached",
            id=operation_id,
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        assert await started.result() == "detached"

        # The strict codec rejects a marked result decoded without the operation's context
        detached = client.get_nexus_operation_handle(operation_id, result_type=str)
        assert await detached.result() == "detached"


@pytest.mark.requires_local_server
async def test_standalone_nexus_payload_codec_receives_context(
    env: WorkflowEnvironment,
):
    """Nexus payload codecs get context for standalone inputs and results."""
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    task_queue = "standalone-nexus-context-codec-task-queue"
    red_endpoint_name = "standalone-red-nexus-endpoint"
    blue_endpoint_name = "standalone-blue-nexus-endpoint"
    red_context = NexusSerializationContext(
        endpoint=red_endpoint_name,
        service="NexusOperationTestServiceHandler",
        operation="operation",
    )
    blue_context = NexusSerializationContext(
        endpoint=blue_endpoint_name,
        service="NexusOperationTestServiceHandler",
        operation="operation",
    )
    config = env.client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_codec=NexusContextMarkerPayloadCodec(
            {red_context: b"red", blue_context: b"blue"}
        ),
    )
    result_interceptor = _NexusResultDecodingInterceptor()
    config["interceptors"] = list(config.get("interceptors") or []) + [
        result_interceptor
    ]
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[NexusOperationTestServiceHandler()],
    ) as worker:
        await env.create_nexus_endpoint(red_endpoint_name, worker.task_queue)
        await env.create_nexus_endpoint(blue_endpoint_name, worker.task_queue)
        red_standalone_result, blue_standalone_result = await asyncio.gather(
            client.create_nexus_client(
                service=NexusOperationTestServiceHandler,
                endpoint=red_endpoint_name,
            ).execute_operation(
                NexusOperationTestServiceHandler.operation,
                "standalone-red",
                id=str(uuid.uuid4()),
                schedule_to_close_timeout=timedelta(seconds=10),
            ),
            client.create_nexus_client(
                service=NexusOperationTestServiceHandler,
                endpoint=blue_endpoint_name,
            ).execute_operation(
                NexusOperationTestServiceHandler.operation,
                "standalone-blue",
                id=str(uuid.uuid4()),
                schedule_to_close_timeout=timedelta(seconds=10),
            ),
        )
        assert red_standalone_result == "standalone-red"
        assert blue_standalone_result == "standalone-blue"
        assert set(result_interceptor.decoded_results) == {
            "standalone-red",
            "standalone-blue",
        }


@pytest.mark.requires_local_server
async def test_workflow_nexus_failure_converter_has_context(
    env: WorkflowEnvironment,
):
    """Workflow Nexus callers and handlers use context for failures."""
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    nexus_failure_context_traces.clear()
    task_queue = "workflow-nexus-failure-context-task-queue"
    endpoint_name = "workflow-failure-nexus-endpoint"
    expected_context = NexusSerializationContext(
        endpoint=endpoint_name,
        service="NexusOperationTestServiceHandler",
        operation="fail",
    )
    config = env.client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        failure_converter_class=NexusFailureConverterWithContext,
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[NexusOperationFailureTestWorkflow],
        nexus_service_handlers=[NexusOperationTestServiceHandler()],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ) as worker:
        await env.create_nexus_endpoint(endpoint_name, worker.task_queue)
        await client.execute_workflow(
            NexusOperationFailureTestWorkflow.run,
            endpoint_name,
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )

        assert ("to_failure", expected_context) in nexus_failure_context_traces
        assert ("from_failure", expected_context) in nexus_failure_context_traces


class NexusContextRequiringPayloadCodec(PayloadCodec, WithSerializationContext):
    """Marks payloads it encodes under a Nexus context and requires that mark back on decode.

    Stands in for a codec keyed on the context, such as one deriving an encryption key from it:
    such a codec cannot recover a payload whose two halves were converted under different
    contexts. Raising in both directions catches a mismatch either way round. The leniency in
    :py:class:`NexusContextMarkerPayloadCodec` deliberately tolerates such a mismatch, so it
    cannot detect this.
    """

    MARKER_KEY = "nexus-context-required-marker"

    def __init__(self, context: SerializationContext | None = None):
        self.context = context

    def with_context(
        self, context: SerializationContext
    ) -> NexusContextRequiringPayloadCodec:
        return NexusContextRequiringPayloadCodec(context)

    def _nexus_context(self) -> NexusSerializationContext | None:
        return (
            self.context
            if isinstance(self.context, NexusSerializationContext)
            else None
        )

    async def encode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        context = self._nexus_context()
        if context is None:
            return list(payloads)
        encoded = []
        for payload in payloads:
            marked = temporalio.api.common.v1.Payload()
            marked.CopyFrom(payload)
            marked.metadata[self.MARKER_KEY] = b"1"
            encoded.append(marked)
        return encoded

    async def decode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        context = self._nexus_context()
        decoded = []
        for payload in payloads:
            marked = self.MARKER_KEY in payload.metadata
            if context is None:
                if marked:
                    raise RuntimeError(
                        "payload encoded under a Nexus context was decoded without one"
                    )
                decoded.append(payload)
                continue
            if not marked:
                raise RuntimeError(
                    f"payload encoded without a Nexus context was decoded under {context!r}"
                )
            stripped = temporalio.api.common.v1.Payload()
            stripped.CopyFrom(payload)
            del stripped.metadata[self.MARKER_KEY]
            decoded.append(stripped)
        return decoded


@pytest.mark.requires_local_server
async def test_standalone_nexus_describe_reads_metadata_with_context(
    env: WorkflowEnvironment,
):
    """A description reads back the summary the start request attached.

    A Nexus operation's user metadata is serialized with the operation's context, the same way
    workflow and activity user metadata is serialized with theirs. Encoding and decoding it under
    different contexts does not round-trip for a converter that varies by context.
    """
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    task_queue = "standalone-nexus-describe-metadata-task-queue"
    endpoint_name = "standalone-describe-metadata-nexus-endpoint"
    config = env.client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_codec=NexusContextRequiringPayloadCodec(),
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[NexusOperationTestServiceHandler()],
    ) as worker:
        await env.create_nexus_endpoint(endpoint_name, worker.task_queue)
        nexus_client = client.create_nexus_client(
            service=NexusOperationTestServiceHandler,
            endpoint=endpoint_name,
        )
        operation_handle = await nexus_client.start_operation(
            NexusOperationTestServiceHandler.operation,
            "describe-metadata",
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=10),
            summary="the-summary",
        )
        assert await operation_handle.result() == "describe-metadata"

        description = await operation_handle.describe()
        assert await description.static_summary() == "the-summary"


@pytest.mark.requires_local_server
async def test_standalone_nexus_failure_converter_has_context(
    env: WorkflowEnvironment,
):
    """Standalone Nexus callers and handlers use context for failures."""
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    nexus_failure_context_traces.clear()
    task_queue = "standalone-nexus-failure-context-task-queue"
    endpoint_name = "standalone-failure-nexus-endpoint"
    expected_context = NexusSerializationContext(
        endpoint=endpoint_name,
        service="NexusOperationTestServiceHandler",
        operation="fail",
    )
    config = env.client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        failure_converter_class=NexusFailureConverterWithContext,
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[NexusOperationTestServiceHandler()],
    ) as worker:
        await env.create_nexus_endpoint(endpoint_name, worker.task_queue)
        nexus_client = client.create_nexus_client(
            service=NexusOperationTestServiceHandler,
            endpoint=endpoint_name,
        )
        operation_handle = await nexus_client.start_operation(
            NexusOperationTestServiceHandler.fail,
            "nexus-failure",
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        with pytest.raises(NexusOperationFailureError):
            await operation_handle.result()

        assert ("to_failure", expected_context) in nexus_failure_context_traces
        assert ("from_failure", expected_context) in nexus_failure_context_traces

        nexus_failure_context_traces.clear()
        description = await operation_handle.describe()
        assert description.last_attempt_failure is not None
        assert ("from_failure", expected_context) in nexus_failure_context_traces


# Test pydantic converter with context


class PydanticData(BaseModel):
    value: str
    trace: list[str] = []


class PydanticJSONConverterWithContext(
    PydanticJSONPlainPayloadConverter, WithSerializationContext
):
    def __init__(self):
        super().__init__()
        self.context: SerializationContext | None = None

    def with_context(
        self, context: SerializationContext | None
    ) -> PydanticJSONConverterWithContext:
        converter = PydanticJSONConverterWithContext()
        converter.context = context
        return converter

    def to_payload(self, value: Any) -> temporalio.api.common.v1.Payload | None:
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
        self.context: SerializationContext | None = None


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


# Test customized DefaultPayloadConverter

# The SDK's CompositePayloadConverter comes with a with_context implementation that ensures that its
# component EncodingPayloadConverters will be replaced with the results of calling with_context() on
# them, if they support with_context (this happens when we call data_converter._with_context). In
# this test, the user has subclassed CompositePayloadConverter. The test confirms that the
# CompositePayloadConverter's with_context yields an instance of the user's subclass.


class UserMethodCalledError(Exception):
    pass


class CustomEncodingPayloadConverter(
    JSONPlainPayloadConverter, WithSerializationContext
):
    @property
    def encoding(self) -> str:
        return "custom-encoding-that-does-not-clash-with-default-converters"

    def __init__(self):
        super().__init__()
        self.context: SerializationContext | None = None

    def with_context(
        self, context: SerializationContext | None
    ) -> CustomEncodingPayloadConverter:
        converter = CustomEncodingPayloadConverter()
        converter.context = context
        return converter


class CustomPayloadConverter(CompositePayloadConverter):
    def __init__(self):
        # Add a context-aware EncodingPayloadConverter so that
        # CompositePayloadConverter.with_context is forced to construct and return a new instance.
        super().__init__(
            CustomEncodingPayloadConverter(),
            *DefaultPayloadConverter.default_encoding_payload_converters,
        )

    def to_payloads(
        self, values: Sequence[Any]
    ) -> list[temporalio.api.common.v1.Payload]:
        raise UserMethodCalledError

    def from_payloads(
        self,
        payloads: Sequence[temporalio.api.common.v1.Payload],
        type_hints: list[type] | None = None,
    ) -> list[Any]:
        raise NotImplementedError


async def test_user_customization_of_default_payload_converter(
    client: Client,
):
    wf_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    client_config = client.config()
    client_config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_converter_class=CustomPayloadConverter,
    )
    client = Client(**client_config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[EchoWorkflow],
    ):
        with pytest.raises(UserMethodCalledError):
            await client.execute_workflow(
                EchoWorkflow.run,
                TraceData(),
                id=wf_id,
                task_queue=task_queue,
            )


class _ContextRecordingCodec(PayloadCodec, WithSerializationContext):
    """Records the serialization context it is handed, so a test can assert which one was used."""

    def __init__(
        self,
        seen: list[SerializationContext | None],
        context: SerializationContext | None = None,
    ):
        self.seen = seen
        self.context = context

    def with_context(self, context: SerializationContext) -> _ContextRecordingCodec:
        return _ContextRecordingCodec(self.seen, context)

    async def encode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        self.seen.append(self.context)
        return list(payloads)

    async def decode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        self.seen.append(self.context)
        return list(payloads)


def _nexus_worker_with_recording_codec(seen: list[SerializationContext | None]):
    """A _NexusWorker with just enough wired up to call _data_converter_for_nexus_task."""
    from unittest import mock

    from temporalio.worker._nexus import _NexusWorker

    return _NexusWorker(
        bridge_worker=lambda: mock.MagicMock(),
        client=mock.MagicMock(namespace="ns"),
        namespace="ns",
        task_queue="tq",
        service_handlers=[NexusOperationTestServiceHandler()],
        data_converter=dataclasses.replace(
            DataConverter.default, payload_codec=_ContextRecordingCodec(seen)
        ),
        interceptors=[],
        metric_meter=mock.MagicMock(),
        executor=None,
    )


async def _encode_through_codec(data_converter: DataConverter) -> None:
    """Drive the converter's codec so it records the context it was scoped to."""
    assert data_converter.payload_codec is not None
    await data_converter.payload_codec.encode([])


async def test_nexus_worker_task_without_endpoint_is_scoped_by_an_empty_endpoint():
    """Servers before 1.30.0 do not report the endpoint a task was addressed to.

    The task is still scoped by service and operation, with an empty endpoint. It will not agree
    with the caller, which scoped by the real endpoint, but it is a Nexus context rather than an
    absent one.
    """
    seen: list[SerializationContext | None] = []
    worker = _nexus_worker_with_recording_codec(seen)

    await _encode_through_codec(
        worker._data_converter_for_nexus_task("", "Service", "operation")
    )
    assert seen == [
        NexusSerializationContext(endpoint="", service="Service", operation="operation")
    ]

    other_seen: list[SerializationContext | None] = []
    other = _nexus_worker_with_recording_codec(other_seen)
    await _encode_through_codec(
        other._data_converter_for_nexus_task("endpoint", "Service", "operation")
    )
    assert other_seen == [
        NexusSerializationContext(
            endpoint="endpoint", service="Service", operation="operation"
        )
    ]


@dataclass
class NexusStartedInput:
    id: str
    value: str
    outcome: Literal["result", "failure", "continue_as_new", "async"] = "result"


@workflow.defn
class NexusStartedWorkflow:
    @workflow.run
    async def run(self, input: NexusStartedInput) -> str:
        if input.outcome == "continue_as_new":
            workflow.continue_as_new(dataclasses.replace(input, outcome="result"))
        if input.outcome == "failure":
            raise ApplicationError(
                "nexus-started-failure", "failure-detail", non_retryable=True
            )
        return input.value


nexus_started_activity_contexts: list[NexusSerializationContext | None] = []
nexus_started_async_completions: list[
    tuple[bytes, NexusSerializationContext | None]
] = []


@activity.defn
async def nexus_started_activity(input: NexusStartedInput) -> str:
    info = activity.info()
    nexus_started_activity_contexts.append(info.nexus_serialization_context)
    if input.outcome == "failure":
        raise ApplicationError(
            "nexus-started-failure", "failure-detail", non_retryable=True
        )
    if input.outcome == "async":
        nexus_started_async_completions.append(
            (info.task_token, info.nexus_serialization_context)
        )
        activity.raise_complete_async()
    if info.attempt == 1:
        # The next attempt decodes these details with the same context
        activity.heartbeat("heartbeat-detail")
        raise ApplicationError("retry")
    assert list(info.heartbeat_details) == ["heartbeat-detail"]
    return input.value


@dataclass
class NexusUpdateInput:
    workflow_id: str
    update_id: str
    value: Literal["result", "failure", "rejection"]


@workflow.defn
class NexusUpdatedWorkflow:
    def __init__(self) -> None:
        self.finished = False

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.finished)

    @workflow.update
    async def update(self, value: str) -> str:
        if value == "failure":
            raise ApplicationError(
                "nexus-update-failure", "failure-detail", non_retryable=True
            )
        return value

    @update.validator
    def validate_update(self, value: str) -> None:
        if value == "rejection":
            raise ApplicationError("nexus-update-rejection", "rejection-detail")

    @workflow.signal
    def finish(self) -> None:
        self.finished = True


@nexusrpc.handler.service_handler
class NexusStartedExecutionHandler:
    @nexus.temporal_operation
    async def update_workflow(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: NexusUpdateInput,
    ) -> nexus.TemporalOperationResult[str]:
        return await client.start_workflow_update(
            input.workflow_id,
            NexusUpdatedWorkflow.update,
            input.value,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            update_id=input.update_id,
        )

    @nexus.temporal_operation
    async def start_workflow(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: NexusStartedInput,
    ) -> nexus.TemporalOperationResult[str]:
        return await client.start_workflow(NexusStartedWorkflow.run, input, id=input.id)

    @nexus.temporal_operation
    async def start_activity(
        self,
        _ctx: nexus.TemporalStartOperationContext,
        client: nexus.TemporalNexusClient,
        input: NexusStartedInput,
    ) -> nexus.TemporalOperationResult[str]:
        return await client.start_activity(
            nexus_started_activity,
            input,
            id=input.id,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=100), maximum_attempts=2
            ),
        )


def _nexus_started_context(
    endpoint_name: str, operation: str
) -> NexusSerializationContext:
    return NexusSerializationContext(
        endpoint=endpoint_name,
        service="NexusStartedExecutionHandler",
        operation=operation,
    )


def _nexus_started_client(
    env: WorkflowEnvironment, context: NexusSerializationContext
) -> Client:
    config = env.client.config()
    config["data_converter"] = dataclasses.replace(
        DataConverter.default,
        payload_codec=NexusContextMarkerPayloadCodec({context: b"nexus"}),
    )
    return Client(**config)


def _marker(payload: temporalio.api.common.v1.Payload) -> bytes | None:
    return payload.metadata.get(NexusContextMarkerPayloadCodec.MARKER_KEY)


async def test_workflow_result_keeps_nexus_context_across_history_pages():
    """The server may omit the context on later history pages, such as the page holding the
    close event after a long poll times out with no events."""
    from unittest import mock

    context = NexusSerializationContext(
        endpoint="endpoint", service="service", operation="operation"
    )
    result = temporalio.api.common.v1.Payload(
        metadata={
            "encoding": b"json/plain",
            NexusContextMarkerPayloadCodec.MARKER_KEY: b"nexus",
        },
        data=b'"nexus-value"',
    )
    first_page = temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryResponse(
        next_page_token=b"next-page",
        propagated_nexus_serialization_context=temporalio.api.nexus.v1.PropagatedSerializationContext(
            endpoint=context.endpoint,
            service=context.service,
            operation=context.operation,
        ),
    )
    close_page = temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryResponse(
        history=temporalio.api.history.v1.History(
            events=[
                temporalio.api.history.v1.HistoryEvent(
                    workflow_execution_completed_event_attributes=temporalio.api.history.v1.WorkflowExecutionCompletedEventAttributes(
                        result=temporalio.api.common.v1.Payloads(payloads=[result])
                    )
                )
            ]
        )
    )
    service_client = mock.MagicMock()
    service_client.workflow_service.get_workflow_execution_history = mock.AsyncMock(
        side_effect=[first_page, close_page]
    )
    client = Client(
        service_client,
        data_converter=dataclasses.replace(
            DataConverter.default,
            payload_codec=NexusContextMarkerPayloadCodec({context: b"nexus"}),
        ),
    )

    assert (
        await client.get_workflow_handle("workflow-id", result_type=str).result()
        == "nexus-value"
    )


@pytest.mark.requires_local_server
@pytest.mark.parametrize("outcome", ["result", "failure", "continue_as_new"])
async def test_nexus_started_workflow_uses_nexus_context(
    env: WorkflowEnvironment,
    outcome: Literal["result", "failure", "continue_as_new"],
):
    """A workflow started by a Nexus handler encodes its result and closing failure with the
    handler's context, and clients decode them with the context the server returns."""
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    context = _nexus_started_context(endpoint_name, "start_workflow")
    client = _nexus_started_client(env, context)
    workflow_id = f"nexus-started-workflow-{uuid.uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[NexusStartedWorkflow],
        nexus_service_handlers=[NexusStartedExecutionHandler()],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        nexus_client = client.create_nexus_client(
            NexusStartedExecutionHandler, endpoint_name
        )
        input = NexusStartedInput(id=workflow_id, value="nexus-value", outcome=outcome)
        # This handle never started the workflow, so it relies on the context the server
        # returns, including for the run after continue-as-new
        handle = client.get_workflow_handle(workflow_id, result_type=str)
        if outcome == "failure":
            with pytest.raises(NexusOperationFailureError):
                await nexus_client.execute_operation(
                    NexusStartedExecutionHandler.start_workflow,
                    input,
                    id=str(uuid.uuid4()),
                )
            with pytest.raises(WorkflowFailureError) as err:
                await handle.result()
            assert isinstance(err.value.cause, ApplicationError)
            assert err.value.cause.details == ("failure-detail",)
        else:
            assert (
                await nexus_client.execute_operation(
                    NexusStartedExecutionHandler.start_workflow,
                    input,
                    id=str(uuid.uuid4()),
                )
                == "nexus-value"
            )
            assert await handle.result() == "nexus-value"

        history = await handle.fetch_history()
        started = history.events[0].workflow_execution_started_event_attributes
        assert (
            started.propagated_nexus_serialization_context
            == temporalio.api.nexus.v1.PropagatedSerializationContext(
                endpoint=context.endpoint,
                service=context.service,
                operation=context.operation,
            )
        )
        # The input keeps the workflow context
        assert all(_marker(p) is None for p in started.input.payloads)
        close_event = history.events[-1]
        if outcome == "failure":
            failure = close_event.workflow_execution_failed_event_attributes.failure
            [detail] = failure.application_failure_info.details.payloads
            assert _marker(detail) == b"nexus"
        else:
            [result] = (
                close_event.workflow_execution_completed_event_attributes.result.payloads
            )
            assert _marker(result) == b"nexus"


@pytest.mark.requires_local_server
async def test_nexus_started_activity_uses_nexus_context(env: WorkflowEnvironment):
    """A standalone activity started by a Nexus handler encodes its result, failures and
    heartbeat details with the handler's context, and clients decode them with the context the
    server returns."""
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    context = _nexus_started_context(endpoint_name, "start_activity")
    client = _nexus_started_client(env, context)
    activity_id = f"nexus-started-activity-{uuid.uuid4()}"
    nexus_started_activity_contexts.clear()

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[nexus_started_activity],
        nexus_service_handlers=[NexusStartedExecutionHandler()],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        result = await client.create_nexus_client(
            NexusStartedExecutionHandler, endpoint_name
        ).execute_operation(
            NexusStartedExecutionHandler.start_activity,
            NexusStartedInput(id=activity_id, value="nexus-value"),
            id=str(uuid.uuid4()),
        )
        assert result == "nexus-value"
        # Both attempts saw the context, and the second decoded the first's heartbeat details
        assert nexus_started_activity_contexts == [context, context]

        handle = client.get_activity_handle(activity_id, result_type=str)
        assert await handle.result() == "nexus-value"
        desc = await handle.describe(
            include_outcome=True,
            include_heartbeat_details=True,
            include_last_failure=True,
        )
        assert await desc.result(str) == "nexus-value"
        assert await desc.heartbeat_details() == ["heartbeat-detail"]
        assert isinstance(await desc.last_failure(), ApplicationError)
        assert desc.raw_outcome is not None
        assert _marker(desc.raw_outcome.result.payloads[0]) == b"nexus"
        assert _marker(desc.raw_info.heartbeat_details.payloads[0]) == b"nexus"


@pytest.mark.requires_local_server
async def test_nexus_started_activity_failure_uses_nexus_context(
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    client = _nexus_started_client(
        env, _nexus_started_context(endpoint_name, "start_activity")
    )
    activity_id = f"nexus-started-activity-{uuid.uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[nexus_started_activity],
        nexus_service_handlers=[NexusStartedExecutionHandler()],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        with pytest.raises(NexusOperationFailureError):
            await client.create_nexus_client(
                NexusStartedExecutionHandler, endpoint_name
            ).execute_operation(
                NexusStartedExecutionHandler.start_activity,
                NexusStartedInput(id=activity_id, value="", outcome="failure"),
                id=str(uuid.uuid4()),
            )

        handle = client.get_activity_handle(activity_id, result_type=str)
        with pytest.raises(ActivityFailureError) as err:
            await handle.result()
        assert isinstance(err.value.cause, ApplicationError)
        assert err.value.cause.details == ("failure-detail",)
        desc = await handle.describe(include_outcome=True)
        outcome_failure = await desc.outcome_failure()
        assert isinstance(outcome_failure, ApplicationError)
        assert outcome_failure.details == ("failure-detail",)


@pytest.mark.requires_local_server
async def test_nexus_started_activity_async_completion_with_context(
    env: WorkflowEnvironment,
):
    """An activity started by a Nexus handler and completed asynchronously uses the context the
    completing client passes in from the activity info."""
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    context = _nexus_started_context(endpoint_name, "start_activity")
    client = _nexus_started_client(env, context)
    activity_id = f"nexus-started-activity-{uuid.uuid4()}"
    nexus_started_async_completions.clear()

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[nexus_started_activity],
        nexus_service_handlers=[NexusStartedExecutionHandler()],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        operation = await client.create_nexus_client(
            NexusStartedExecutionHandler, endpoint_name
        ).start_operation(
            NexusStartedExecutionHandler.start_activity,
            NexusStartedInput(id=activity_id, value="", outcome="async"),
            id=str(uuid.uuid4()),
        )

        async def activity_started() -> bool:
            return bool(nexus_started_async_completions)

        await assert_eq_eventually(True, activity_started)
        [(task_token, info_context)] = nexus_started_async_completions
        assert info_context is not None
        assert info_context == context
        async_handle = client.get_async_activity_handle(
            task_token=task_token
        ).with_context(info_context)
        await async_handle.heartbeat("client-heartbeat")
        desc = await client.get_activity_handle(activity_id).describe(
            include_heartbeat_details=True
        )
        assert await desc.heartbeat_details() == ["client-heartbeat"]
        assert _marker(desc.raw_info.heartbeat_details.payloads[0]) == b"nexus"
        await async_handle.complete("async-value")
        assert await operation.result() == "async-value"
        assert (
            await client.get_activity_handle(activity_id, result_type=str).result()
            == "async-value"
        )


class _UpdateOutcomeCapturingInterceptor(Interceptor):
    def __init__(self) -> None:
        super().__init__()
        self.outcomes: list[temporalio.api.update.v1.Outcome] = []

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return _UpdateOutcomeCapturingOutboundInterceptor(next, self)


class _UpdateOutcomeCapturingOutboundInterceptor(OutboundInterceptor):
    def __init__(
        self, next: OutboundInterceptor, parent: _UpdateOutcomeCapturingInterceptor
    ) -> None:
        super().__init__(next)
        self._parent = parent

    async def start_workflow_update(
        self, input: StartWorkflowUpdateInput
    ) -> WorkflowUpdateHandle[Any]:
        handle = await super().start_workflow_update(input)
        if handle._known_outcome:
            # Copy, since decoding the outcome's failure later modifies it in place
            outcome = temporalio.api.update.v1.Outcome()
            outcome.CopyFrom(handle._known_outcome)
            self._parent.outcomes.append(outcome)
        return handle


@pytest.mark.requires_local_server
@pytest.mark.parametrize("value", ["result", "failure", "rejection"])
async def test_nexus_sent_update_uses_nexus_context(
    env: WorkflowEnvironment,
    value: Literal["result", "failure", "rejection"],
):
    """An update sent by a Nexus handler encodes its result, failure and rejection with the
    handler's context, and clients decode them with the context the server returns."""
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with the Java test server")

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)
    context = _nexus_started_context(endpoint_name, "update_workflow")
    # Rejected updates aren't stored in history, so the test reads the rejection from the
    # update response the handler receives
    outcome_capture = _UpdateOutcomeCapturingInterceptor()
    config = _nexus_started_client(env, context).config()
    config["interceptors"] = [outcome_capture]
    client = Client(**config)
    update_id = str(uuid.uuid4())

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[NexusUpdatedWorkflow],
        nexus_service_handlers=[NexusStartedExecutionHandler()],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)
        handle = await client.start_workflow(
            NexusUpdatedWorkflow.run,
            id=f"nexus-updated-workflow-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        operation = client.create_nexus_client(
            NexusStartedExecutionHandler, endpoint_name
        ).execute_operation(
            NexusStartedExecutionHandler.update_workflow,
            NexusUpdateInput(workflow_id=handle.id, update_id=update_id, value=value),
            id=str(uuid.uuid4()),
        )
        if value == "result":
            assert await operation == "result"
        else:
            with pytest.raises(NexusOperationFailureError):
                await operation

        if value == "rejection":
            [outcome] = outcome_capture.outcomes
            [detail] = outcome.failure.application_failure_info.details.payloads
            assert _marker(detail) == b"nexus"
        else:
            # This handle never sent the update, so it relies on the context the server returns
            update_handle = handle.get_update_handle(update_id, result_type=str)
            if value == "result":
                assert await update_handle.result() == "result"
            else:
                with pytest.raises(WorkflowUpdateFailedError) as err:
                    await update_handle.result()
                assert isinstance(err.value.cause, ApplicationError)
                assert err.value.cause.details == ("failure-detail",)

        await handle.signal(NexusUpdatedWorkflow.finish)
        await handle.result()

        history = await handle.fetch_history()
        if value != "rejection":
            [accepted] = [
                e.workflow_execution_update_accepted_event_attributes
                for e in history.events
                if e.HasField("workflow_execution_update_accepted_event_attributes")
            ]
            assert (
                accepted.accepted_request.propagated_nexus_serialization_context
                == temporalio.api.nexus.v1.PropagatedSerializationContext(
                    endpoint=context.endpoint,
                    service=context.service,
                    operation=context.operation,
                )
            )
            # The input keeps the workflow context
            assert all(
                _marker(p) is None
                for p in accepted.accepted_request.input.args.payloads
            )
            [completed] = [
                e.workflow_execution_update_completed_event_attributes
                for e in history.events
                if e.HasField("workflow_execution_update_completed_event_attributes")
            ]
            if value == "result":
                [result] = completed.outcome.success.payloads
                assert _marker(result) == b"nexus"
            else:
                failure = completed.outcome.failure
                [detail] = failure.application_failure_info.details.payloads
                assert _marker(detail) == b"nexus"

    # Replaying re-runs the update handler with the context from history
    await Replayer(
        workflows=[NexusUpdatedWorkflow], data_converter=client.data_converter
    ).replay_workflow(history)
