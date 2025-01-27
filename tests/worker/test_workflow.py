from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import logging.handlers
import queue
import sys
import threading
import typing
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from functools import partial
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
)
from urllib.request import urlopen

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from typing_extensions import Literal, Protocol, runtime_checkable

import temporalio.worker
import temporalio.workflow
from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload, Payloads, WorkflowExecution
from temporalio.api.enums.v1 import EventType
from temporalio.api.failure.v1 import Failure
from temporalio.api.sdk.v1 import EnhancedStackTrace
from temporalio.api.workflowservice.v1 import (
    GetWorkflowExecutionHistoryRequest,
    ResetStickyTaskQueueRequest,
)
from temporalio.bridge.proto.workflow_activation import WorkflowActivation
from temporalio.bridge.proto.workflow_completion import WorkflowActivationCompletion
from temporalio.client import (
    Client,
    RPCError,
    RPCStatusCode,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowQueryFailedError,
    WorkflowUpdateFailedError,
    WorkflowUpdateHandle,
    WorkflowUpdateRPCTimeoutOrCancelledError,
    WorkflowUpdateStage,
)
from temporalio.common import (
    RawValue,
    RetryPolicy,
    SearchAttributeKey,
    SearchAttributePair,
    SearchAttributes,
    SearchAttributeValues,
    TypedSearchAttributes,
    WorkflowIDConflictPolicy,
)
from temporalio.converter import (
    DataConverter,
    DefaultFailureConverter,
    DefaultFailureConverterWithEncodedAttributes,
    DefaultPayloadConverter,
    PayloadCodec,
    PayloadConverter,
)
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    ChildWorkflowError,
    TemporalError,
    TimeoutError,
    WorkflowAlreadyStartedError,
)
from temporalio.runtime import (
    BUFFERED_METRIC_KIND_COUNTER,
    BUFFERED_METRIC_KIND_HISTOGRAM,
    MetricBuffer,
    MetricBufferDurationFormat,
    PrometheusConfig,
    Runtime,
    TelemetryConfig,
)
from temporalio.service import RPCError, RPCStatusCode, __version__
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    UnsandboxedWorkflowRunner,
    Worker,
    WorkflowInstance,
    WorkflowInstanceDetails,
    WorkflowRunner,
)
from tests.helpers import (
    admitted_update_task,
    assert_eq_eventually,
    ensure_search_attributes_present,
    find_free_port,
    new_worker,
    workflow_update_exists,
)
from tests.helpers.external_stack_trace import (
    ExternalStackTraceWorkflow,
    external_wait_cancel,
)


@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"


async def test_workflow_hello(client: Client):
    async with new_worker(client, HelloWorkflow) as worker:
        result = await client.execute_workflow(
            HelloWorkflow.run,
            "Temporal",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result == "Hello, Temporal!"


async def test_workflow_hello_eager(client: Client):
    async with new_worker(client, HelloWorkflow) as worker:
        handle = await client.start_workflow(
            HelloWorkflow.run,
            "Temporal",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            request_eager_start=True,
            task_timeout=timedelta(hours=1),  # hang if retry needed
        )
        assert handle.__temporal_eagerly_started
        result = await handle.result()
        assert result == "Hello, Temporal!"


@activity.defn
async def multi_param_activity(param1: int, param2: str) -> str:
    return f"param1: {param1}, param2: {param2}"


@workflow.defn
class MultiParamWorkflow:
    @workflow.run
    async def run(self, param1: int, param2: str) -> str:
        return await workflow.execute_activity(
            multi_param_activity,
            args=[param1, param2],
            schedule_to_close_timeout=timedelta(seconds=30),
        )


async def test_workflow_multi_param(client: Client):
    # This test is mostly just here to confirm MyPy type checks the multi-param
    # overload approach properly
    async with new_worker(
        client, MultiParamWorkflow, activities=[multi_param_activity]
    ) as worker:
        result = await client.execute_workflow(
            MultiParamWorkflow.run,
            args=[123, "val1"],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result == "param1: 123, param2: val1"


@workflow.defn
class InfoWorkflow:
    @workflow.run
    async def run(self) -> Dict:
        # Convert to JSON and back so it'll stringify un-JSON-able pieces
        ret = dataclasses.asdict(workflow.info())
        return json.loads(json.dumps(ret, default=str))


async def test_workflow_info(client: Client, env: WorkflowEnvironment):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1426"
        )
    async with new_worker(client, InfoWorkflow) as worker:
        workflow_id = f"workflow-{uuid.uuid4()}"
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=3),
            backoff_coefficient=4.0,
            maximum_interval=timedelta(seconds=5),
            maximum_attempts=6,
        )
        info = await client.execute_workflow(
            InfoWorkflow.run,
            id=workflow_id,
            task_queue=worker.task_queue,
            retry_policy=retry_policy,
        )
        assert info["attempt"] == 1
        assert info["cron_schedule"] is None
        assert info["execution_timeout"] is None
        assert info["namespace"] == client.namespace
        assert info["retry_policy"] == json.loads(
            json.dumps(dataclasses.asdict(retry_policy), default=str)
        )
        assert uuid.UUID(info["run_id"]).version == 4
        assert info["run_timeout"] is None
        datetime.fromisoformat(info["start_time"])
        assert info["task_queue"] == worker.task_queue
        assert info["task_timeout"] == "0:00:10"
        assert info["workflow_id"] == workflow_id
        assert info["workflow_type"] == "InfoWorkflow"


@dataclass
class HistoryInfo:
    history_length: int
    history_size: int
    continue_as_new_suggested: bool


@workflow.defn
class HistoryInfoWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Just wait forever
        await workflow.wait_condition(lambda: False)

    @workflow.signal
    async def bunch_of_events(self, count: int) -> None:
        # Create a lot of one-day timers
        for _ in range(count):
            asyncio.create_task(asyncio.sleep(60 * 60 * 24))

    @workflow.query
    def get_history_info(self) -> HistoryInfo:
        return HistoryInfo(
            history_length=workflow.info().get_current_history_length(),
            history_size=workflow.info().get_current_history_size(),
            continue_as_new_suggested=workflow.info().is_continue_as_new_suggested(),
        )


async def test_workflow_history_info(
    client: Client, env: WorkflowEnvironment, continue_as_new_suggest_history_count: int
):
    if env.supports_time_skipping:
        pytest.skip("Java test server does not support should continue as new")
    async with new_worker(client, HistoryInfoWorkflow) as worker:
        handle = await client.start_workflow(
            HistoryInfoWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # Issue query before anything else, which should mean only a history
        # size of 3, at least 100 bytes of history, and no continue as new
        # suggestion
        orig_info = await handle.query(HistoryInfoWorkflow.get_history_info)
        assert orig_info.history_length == 3
        assert orig_info.history_size > 100
        assert not orig_info.continue_as_new_suggested

        # Now send a lot of events
        await handle.signal(
            HistoryInfoWorkflow.bunch_of_events, continue_as_new_suggest_history_count
        )
        # Send one more event to trigger the WFT update. We have to do this
        # because just a query will have a stale representation of history
        # counts, but signal forces a new WFT.
        await handle.signal(HistoryInfoWorkflow.bunch_of_events, 1)
        new_info = await handle.query(HistoryInfoWorkflow.get_history_info)
        assert new_info.history_length > continue_as_new_suggest_history_count
        assert new_info.history_size > orig_info.history_size
        assert new_info.continue_as_new_suggested


@workflow.defn
class SignalAndQueryWorkflow:
    def __init__(self) -> None:
        self._last_event: Optional[str] = None

    @workflow.run
    async def run(self) -> None:
        # Wait forever
        await asyncio.Future()

    @workflow.signal
    def signal1(self, arg: str) -> None:
        self._last_event = f"signal1: {arg}"

    @workflow.signal(dynamic=True)
    def signal_dynamic(self, name: str, args: Sequence[RawValue]) -> None:
        arg = workflow.payload_converter().from_payload(args[0].payload, str)
        self._last_event = f"signal_dynamic {name}: {arg}"

    @workflow.signal(name="Custom Name")
    def signal_custom(self, arg: str) -> None:
        self._last_event = f"signal_custom: {arg}"

    @workflow.query
    def last_event(self) -> str:
        return self._last_event or "<no event>"

    @workflow.query(dynamic=True)
    def query_dynamic(self, name: str, args: Sequence[RawValue]) -> str:
        arg = workflow.payload_converter().from_payload(args[0].payload, str)
        return f"query_dynamic {name}: {arg}"

    @workflow.query(name="Custom Name")
    def query_custom(self, arg: str) -> str:
        return f"query_custom: {arg}"


async def test_workflow_signal_and_query(client: Client):
    async with new_worker(client, SignalAndQueryWorkflow) as worker:
        handle = await client.start_workflow(
            SignalAndQueryWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Simple signals and queries
        await handle.signal(SignalAndQueryWorkflow.signal1, "some arg")
        assert "signal1: some arg" == await handle.query(
            SignalAndQueryWorkflow.last_event
        )

        # Dynamic signals and queries (old form)
        await handle.signal("signal2", "dyn arg")
        assert "signal_dynamic signal2: dyn arg" == await handle.query(
            SignalAndQueryWorkflow.last_event
        )
        assert "query_dynamic query2: dyn arg" == await handle.query(
            "query2", "dyn arg"
        )

        # Custom named signals and queries
        await handle.signal("Custom Name", "custom arg1")
        assert "signal_custom: custom arg1" == await handle.query(
            SignalAndQueryWorkflow.last_event
        )
        await handle.signal(SignalAndQueryWorkflow.signal_custom, "custom arg2")
        assert "signal_custom: custom arg2" == await handle.query(
            SignalAndQueryWorkflow.last_event
        )
        assert "query_custom: custom arg1" == await handle.query(
            "Custom Name", "custom arg1"
        )
        assert "query_custom: custom arg1" == await handle.query(
            SignalAndQueryWorkflow.query_custom, "custom arg1"
        )


@workflow.defn
class SignalAndQueryHandlersWorkflow:
    def __init__(self) -> None:
        self._last_event: Optional[str] = None

    @workflow.run
    async def run(self) -> None:
        # Wait forever
        await asyncio.Future()

    @workflow.query
    def last_event(self) -> str:
        return self._last_event or "<no event>"

    @workflow.signal
    def set_signal_handler(self, signal_name: str) -> None:
        def new_handler(arg: str) -> None:
            self._last_event = f"signal {signal_name}: {arg}"

        workflow.set_signal_handler(signal_name, new_handler)

    @workflow.signal
    def set_query_handler(self, query_name: str) -> None:
        def new_handler(arg: str) -> str:
            return f"query {query_name}: {arg}"

        workflow.set_query_handler(query_name, new_handler)

    @workflow.signal
    def set_dynamic_signal_handler(self) -> None:
        def new_handler(name: str, args: Sequence[RawValue]) -> None:
            arg = workflow.payload_converter().from_payload(args[0].payload, str)
            self._last_event = f"signal dynamic {name}: {arg}"

        workflow.set_dynamic_signal_handler(new_handler)

    @workflow.signal
    def set_dynamic_query_handler(self) -> None:
        def new_handler(name: str, args: Sequence[RawValue]) -> str:
            arg = workflow.payload_converter().from_payload(args[0].payload, str)
            return f"query dynamic {name}: {arg}"

        workflow.set_dynamic_query_handler(new_handler)


async def test_workflow_signal_and_query_handlers(client: Client):
    async with new_worker(client, SignalAndQueryHandlersWorkflow) as worker:
        handle = await client.start_workflow(
            SignalAndQueryHandlersWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Confirm signals buffered when not found
        await handle.signal("unknown_signal1", "val1")
        await handle.signal(
            SignalAndQueryHandlersWorkflow.set_signal_handler, "unknown_signal1"
        )
        assert "signal unknown_signal1: val1" == await handle.query(
            SignalAndQueryHandlersWorkflow.last_event
        )

        # Normal signal handling
        await handle.signal("unknown_signal1", "val2")
        assert "signal unknown_signal1: val2" == await handle.query(
            SignalAndQueryHandlersWorkflow.last_event
        )

        # Dynamic signal handling buffered and new
        await handle.signal("unknown_signal2", "val3")
        await handle.signal(SignalAndQueryHandlersWorkflow.set_dynamic_signal_handler)
        assert "signal dynamic unknown_signal2: val3" == await handle.query(
            SignalAndQueryHandlersWorkflow.last_event
        )
        await handle.signal("unknown_signal3", "val4")
        assert "signal dynamic unknown_signal3: val4" == await handle.query(
            SignalAndQueryHandlersWorkflow.last_event
        )

        # Normal query handling
        await handle.signal(
            SignalAndQueryHandlersWorkflow.set_query_handler, "unknown_query1"
        )
        assert "query unknown_query1: val5" == await handle.query(
            "unknown_query1", "val5"
        )

        # Dynamic query handling
        await handle.signal(SignalAndQueryHandlersWorkflow.set_dynamic_query_handler)
        assert "query dynamic unknown_query2: val6" == await handle.query(
            "unknown_query2", "val6"
        )


@workflow.defn
class SignalAndQueryErrorsWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Wait forever
        await asyncio.Future()

    @workflow.signal
    def bad_signal(self) -> NoReturn:
        raise ApplicationError("signal fail", 123)

    @workflow.query
    def bad_query(self) -> NoReturn:
        raise ApplicationError("query fail", 456)

    @workflow.query
    def other_query(self) -> str:
        raise NotImplementedError


async def test_workflow_signal_and_query_errors(client: Client):
    async with new_worker(client, SignalAndQueryErrorsWorkflow) as worker:
        handle = await client.start_workflow(
            SignalAndQueryErrorsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # Send bad signal
        await handle.signal(SignalAndQueryErrorsWorkflow.bad_signal)
        # Wait on workflow
        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()
        assert isinstance(err.value.cause, ApplicationError)
        assert list(err.value.cause.details) == [123]
        # Fail query (no details on query failure)
        with pytest.raises(WorkflowQueryFailedError) as rpc_err:
            await handle.query(SignalAndQueryErrorsWorkflow.bad_query)
        assert str(rpc_err.value) == "query fail"
        # Unrecognized query
        with pytest.raises(WorkflowQueryFailedError) as rpc_err:
            await handle.query("non-existent query")
        assert str(rpc_err.value) == (
            "Query handler for 'non-existent query' expected but not found,"
            " known queries: [__enhanced_stack_trace __stack_trace __temporal_workflow_metadata bad_query other_query]"
        )


@workflow.defn
class SignalAndQueryOldDynamicStyleWorkflow:
    def __init__(self) -> None:
        self._last_event: Optional[str] = None

    @workflow.run
    async def run(self) -> None:
        # Wait forever
        await asyncio.Future()

    @workflow.signal(dynamic=True)
    def signal_dynamic(self, name: str, *args: Any) -> None:
        self._last_event = f"signal_dynamic {name}: {args[0]}"

    @workflow.query
    def last_event(self) -> str:
        return self._last_event or "<no event>"

    @workflow.query(dynamic=True)
    def query_dynamic(self, name: str, *args: Any) -> str:
        return f"query_dynamic {name}: {args[0]}"


async def test_workflow_signal_and_query_old_dynamic_style(client: Client):
    async with new_worker(client, SignalAndQueryOldDynamicStyleWorkflow) as worker:
        handle = await client.start_workflow(
            SignalAndQueryOldDynamicStyleWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Dynamic signals and queries
        await handle.signal("signal1", "dyn arg")
        assert "signal_dynamic signal1: dyn arg" == await handle.query(
            SignalAndQueryOldDynamicStyleWorkflow.last_event
        )
        assert "query_dynamic query1: dyn arg" == await handle.query(
            "query1", "dyn arg"
        )


@workflow.defn
class SignalAndQueryHandlersOldDynamicStyleWorkflow:
    def __init__(self) -> None:
        self._last_event: Optional[str] = None

    @workflow.run
    async def run(self) -> None:
        # Wait forever
        await asyncio.Future()

    @workflow.query
    def last_event(self) -> str:
        return self._last_event or "<no event>"

    @workflow.signal
    def set_dynamic_signal_handler(self) -> None:
        def new_handler(name: str, *args: Any) -> None:
            self._last_event = f"signal dynamic {name}: {args[0]}"

        workflow.set_dynamic_signal_handler(new_handler)

    @workflow.signal
    def set_dynamic_query_handler(self) -> None:
        def new_handler(name: str, *args: Any) -> str:
            return f"query dynamic {name}: {args[0]}"

        workflow.set_dynamic_query_handler(new_handler)


async def test_workflow_signal_qnd_query_handlers_old_dynamic_style(client: Client):
    async with new_worker(
        client, SignalAndQueryHandlersOldDynamicStyleWorkflow
    ) as worker:
        handle = await client.start_workflow(
            SignalAndQueryHandlersOldDynamicStyleWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Dynamic signal handling buffered and new
        await handle.signal("unknown_signal1", "val1")
        await handle.signal(
            SignalAndQueryHandlersOldDynamicStyleWorkflow.set_dynamic_signal_handler
        )
        assert "signal dynamic unknown_signal1: val1" == await handle.query(
            SignalAndQueryHandlersOldDynamicStyleWorkflow.last_event
        )
        await handle.signal("unknown_signal2", "val2")
        assert "signal dynamic unknown_signal2: val2" == await handle.query(
            SignalAndQueryHandlersOldDynamicStyleWorkflow.last_event
        )

        # Dynamic query handling
        await handle.signal(
            SignalAndQueryHandlersOldDynamicStyleWorkflow.set_dynamic_query_handler
        )
        assert "query dynamic unknown_query1: val3" == await handle.query(
            "unknown_query1", "val3"
        )


@dataclass
class BadSignalParam:
    some_str: str


@workflow.defn
class BadSignalParamWorkflow:
    def __init__(self) -> None:
        self._signals: List[BadSignalParam] = []

    @workflow.run
    async def run(self) -> List[BadSignalParam]:
        await workflow.wait_condition(
            lambda: bool(self._signals) and self._signals[-1].some_str == "finish"
        )
        return self._signals

    @workflow.signal
    async def some_signal(self, param: BadSignalParam) -> None:
        self._signals.append(param)


async def test_workflow_bad_signal_param(client: Client):
    async with new_worker(client, BadSignalParamWorkflow) as worker:
        handle = await client.start_workflow(
            BadSignalParamWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # Send 4 signals, first and third are bad
        await handle.signal("some_signal", "bad")
        await handle.signal("some_signal", BadSignalParam(some_str="good"))
        await handle.signal("some_signal", 123)
        await handle.signal("some_signal", BadSignalParam(some_str="finish"))
        assert [
            BadSignalParam(some_str="good"),
            BadSignalParam(some_str="finish"),
        ] == await handle.result()


@workflow.defn
class AsyncUtilWorkflow:
    def __init__(self) -> None:
        self._status = "starting"
        self._wait_event1 = asyncio.Event()
        self._received_event2 = False

    @workflow.run
    async def run(self) -> Dict:
        # Record start times
        ret = {
            # "now" timestamp and current event loop monotonic time
            "start": str(workflow.now()),
            "start_time": workflow.time(),
            "start_time_ns": workflow.time_ns(),
            "event_loop_start": asyncio.get_running_loop().time(),
        }

        # Sleep for a small amount of time (we accept that it may take longer on
        # the server)
        await asyncio.sleep(0.1)

        # Wait for event 1
        self._status = "waiting for event1"
        await self._wait_event1.wait()

        # Wait for event 2
        self._status = "waiting for event2"
        await workflow.wait_condition(lambda: self._received_event2)

        # Record completion times
        self._status = "done"
        ret["end_time_ns"] = workflow.time_ns()
        return ret

    @workflow.signal
    def event1(self) -> None:
        self._wait_event1.set()

    @workflow.signal
    def event2(self) -> None:
        self._received_event2 = True

    @workflow.query
    def status(self) -> str:
        return self._status


async def test_workflow_async_utils(client: Client):
    async with new_worker(client, AsyncUtilWorkflow) as worker:
        # Start workflow and wait until status is waiting for event 1
        handle = await client.start_workflow(
            AsyncUtilWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        async def status() -> str:
            return await handle.query(AsyncUtilWorkflow.status)

        await assert_eq_eventually("waiting for event1", status)

        # Set event 1 and confirm waiting on event 2
        await handle.signal(AsyncUtilWorkflow.event1)
        await assert_eq_eventually("waiting for event2", status)

        # Set event 2 and get the result and confirm query still works
        await handle.signal(AsyncUtilWorkflow.event2)
        result = await handle.result()
        assert "done" == await status()

        # Get the actual start time out of history
        resp = await client.workflow_service.get_workflow_execution_history(
            GetWorkflowExecutionHistoryRequest(
                namespace=client.namespace,
                execution=WorkflowExecution(workflow_id=handle.id),
            )
        )
        first_timestamp: Optional[Timestamp] = None
        last_timestamp: Optional[Timestamp] = None
        for event in resp.history.events:
            # Get timestamp from first workflow task started
            if event.event_type is EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED:
                if not first_timestamp:
                    first_timestamp = event.event_time
                last_timestamp = event.event_time
        assert first_timestamp and last_timestamp

        # Check the times. We have to ignore type here because typeshed has
        # wrong type for Protobuf ToDatetime.
        first_timestamp_datetime = first_timestamp.ToDatetime(tzinfo=timezone.utc)  # type: ignore
        # We take off subsecond because Protobuf rounds nanos
        # differently than we do (they round toward zero, we use
        # utcfromtimestamp which suffers float precision issues).
        assert datetime.fromisoformat(result["start"]).replace(
            microsecond=0
        ) == first_timestamp_datetime.replace(microsecond=0)
        assert result["start_time"] == first_timestamp.ToNanoseconds() / 1e9
        assert result["start_time_ns"] == first_timestamp.ToNanoseconds()
        assert result["event_loop_start"] == result["start_time"]
        assert result["start_time_ns"] < result["end_time_ns"]
        assert result["end_time_ns"] == last_timestamp.ToNanoseconds()


@activity.defn
async def say_hello(name: str) -> str:
    return f"Hello, {name}!"


@workflow.defn
class SimpleActivityWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello, name, schedule_to_close_timeout=timedelta(seconds=5)
        )


async def test_workflow_simple_activity(client: Client):
    async with new_worker(
        client, SimpleActivityWorkflow, activities=[say_hello]
    ) as worker:
        result = await client.execute_workflow(
            SimpleActivityWorkflow.run,
            "Temporal",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result == "Hello, Temporal!"


@workflow.defn
class SimpleLocalActivityWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_local_activity(
            say_hello, name, schedule_to_close_timeout=timedelta(seconds=5)
        )


async def test_workflow_simple_local_activity(client: Client):
    async with new_worker(
        client, SimpleLocalActivityWorkflow, activities=[say_hello]
    ) as worker:
        result = await client.execute_workflow(
            SimpleLocalActivityWorkflow.run,
            "Temporal",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result == "Hello, Temporal!"


@activity.defn
async def wait_cancel() -> str:
    try:
        if activity.info().is_local:
            await asyncio.sleep(1000)
        else:
            while True:
                await asyncio.sleep(0.3)
                activity.heartbeat()
        return "Manually stopped"
    except asyncio.CancelledError:
        return "Got cancelled error, cancelled? " + str(activity.is_cancelled())


class ActivityWaitCancelNotify:
    def __init__(self) -> None:
        self.wait_cancel_complete = asyncio.Event()

    @activity.defn
    async def wait_cancel(self) -> str:
        self.wait_cancel_complete.clear()
        try:
            if activity.info().is_local:
                await asyncio.sleep(1000)
            else:
                while True:
                    await asyncio.sleep(0.3)
                    activity.heartbeat()
            return "Manually stopped"
        except asyncio.CancelledError:
            return "Got cancelled error, cancelled? " + str(activity.is_cancelled())
        finally:
            self.wait_cancel_complete.set()


@dataclass
class CancelActivityWorkflowParams:
    cancellation_type: str
    local: bool


@workflow.defn
class CancelActivityWorkflow:
    def __init__(self) -> None:
        self._activity_result = "<none>"

    @workflow.run
    async def run(self, params: CancelActivityWorkflowParams) -> None:
        if params.local:
            handle = workflow.start_local_activity_method(
                ActivityWaitCancelNotify.wait_cancel,
                schedule_to_close_timeout=timedelta(seconds=5),
                cancellation_type=workflow.ActivityCancellationType[
                    params.cancellation_type
                ],
            )
        else:
            handle = workflow.start_activity_method(
                ActivityWaitCancelNotify.wait_cancel,
                schedule_to_close_timeout=timedelta(seconds=5),
                heartbeat_timeout=timedelta(seconds=1),
                cancellation_type=workflow.ActivityCancellationType[
                    params.cancellation_type
                ],
            )
        await asyncio.sleep(0.01)
        try:
            handle.cancel()
            self._activity_result = await handle
        except ActivityError as err:
            self._activity_result = f"Error: {err.cause.__class__.__name__}"
        # TODO(cretz): Remove when https://github.com/temporalio/sdk-core/issues/323 is fixed
        except CancelledError as err:
            self._activity_result = f"Error: {err.__class__.__name__}"
        # Wait forever
        await asyncio.Future()

    @workflow.query
    def activity_result(self) -> str:
        return self._activity_result


@pytest.mark.parametrize("local", [True, False])
async def test_workflow_cancel_activity(client: Client, local: bool):
    # Need short task timeout to timeout LA task and longer assert timeout
    # so the task can timeout
    task_timeout = timedelta(seconds=1)
    assert_timeout = timedelta(seconds=10)
    activity_inst = ActivityWaitCancelNotify()

    async with new_worker(
        client, CancelActivityWorkflow, activities=[activity_inst.wait_cancel]
    ) as worker:
        # Try cancel - confirm error and activity was sent the cancel
        handle = await client.start_workflow(
            CancelActivityWorkflow.run,
            CancelActivityWorkflowParams(
                cancellation_type=workflow.ActivityCancellationType.TRY_CANCEL.name,
                local=local,
            ),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            task_timeout=task_timeout,
        )

        async def activity_result() -> str:
            return await handle.query(CancelActivityWorkflow.activity_result)

        await assert_eq_eventually(
            "Error: CancelledError", activity_result, timeout=assert_timeout
        )
        await activity_inst.wait_cancel_complete.wait()
        await handle.cancel()

        # Wait cancel - confirm no error due to graceful cancel handling
        handle = await client.start_workflow(
            CancelActivityWorkflow.run,
            CancelActivityWorkflowParams(
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED.name,
                local=local,
            ),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            task_timeout=task_timeout,
        )
        await assert_eq_eventually(
            "Got cancelled error, cancelled? True",
            activity_result,
            timeout=assert_timeout,
        )
        await activity_inst.wait_cancel_complete.wait()
        await handle.cancel()

        # Abandon - confirm error and that activity stays running
        handle = await client.start_workflow(
            CancelActivityWorkflow.run,
            CancelActivityWorkflowParams(
                cancellation_type=workflow.ActivityCancellationType.ABANDON.name,
                local=local,
            ),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            task_timeout=task_timeout,
        )
        await assert_eq_eventually(
            "Error: CancelledError", activity_result, timeout=assert_timeout
        )
        await asyncio.sleep(0.5)
        assert not activity_inst.wait_cancel_complete.is_set()
        await handle.cancel()
        await activity_inst.wait_cancel_complete.wait()


@workflow.defn
class SimpleChildWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_child_workflow(HelloWorkflow.run, name)


async def test_workflow_simple_child(client: Client):
    async with new_worker(client, SimpleChildWorkflow, HelloWorkflow) as worker:
        result = await client.execute_workflow(
            SimpleChildWorkflow.run,
            "Temporal",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result == "Hello, Temporal!"


@workflow.defn
class LongSleepWorkflow:
    @workflow.run
    async def run(self) -> None:
        self._started = True
        await asyncio.sleep(1000)

    @workflow.query
    def started(self) -> bool:
        return self._started


async def test_workflow_simple_cancel(client: Client):
    async with new_worker(client, LongSleepWorkflow) as worker:
        handle = await client.start_workflow(
            LongSleepWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        async def started() -> bool:
            return await handle.query(LongSleepWorkflow.started)

        await assert_eq_eventually(True, started)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()
        assert isinstance(err.value.cause, CancelledError)
        assert (await handle.describe()).status == WorkflowExecutionStatus.CANCELED


@workflow.defn
class TrapCancelWorkflow:
    @workflow.run
    async def run(self) -> str:
        try:
            await asyncio.Future()
            raise RuntimeError("should not get here")
        except asyncio.CancelledError:
            return "cancelled"


async def test_workflow_cancel_before_run(client: Client):
    # Start the workflow _and_ send cancel before even starting the workflow
    task_queue = str(uuid.uuid4())
    handle = await client.start_workflow(
        TrapCancelWorkflow.run,
        id=f"workflow-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.cancel()
    # Start worker and wait for result
    async with new_worker(client, TrapCancelWorkflow, task_queue=task_queue):
        assert "cancelled" == await handle.result()


@activity.defn
async def wait_forever() -> NoReturn:
    await asyncio.Future()
    raise RuntimeError("Unreachable")


@workflow.defn
class UncaughtCancelWorkflow:
    @workflow.run
    async def run(self, activity: bool) -> NoReturn:
        self._started = True
        # Wait forever on activity or child workflow
        if activity:
            await workflow.execute_activity(
                wait_forever, start_to_close_timeout=timedelta(seconds=1000)
            )
        else:
            await workflow.execute_child_workflow(
                UncaughtCancelWorkflow.run,
                True,
                id=f"{workflow.info().workflow_id}_child",
            )

    @workflow.query
    def started(self) -> bool:
        return self._started


@pytest.mark.parametrize("activity", [True, False])
async def test_workflow_uncaught_cancel(client: Client, activity: bool):
    async with new_worker(
        client, UncaughtCancelWorkflow, activities=[wait_forever]
    ) as worker:
        # Start workflow waiting on activity or child workflow, cancel it, and
        # confirm the workflow is shown as cancelled
        handle = await client.start_workflow(
            UncaughtCancelWorkflow.run,
            activity,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        async def started() -> bool:
            return await handle.query(UncaughtCancelWorkflow.started)

        await assert_eq_eventually(True, started)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()
        assert isinstance(err.value.cause, CancelledError)


@workflow.defn
class CancelChildWorkflow:
    def __init__(self) -> None:
        self._ready = False

    @workflow.run
    async def run(self, use_execute: bool) -> None:
        if use_execute:
            self._task = asyncio.create_task(
                workflow.execute_child_workflow(
                    LongSleepWorkflow.run, id=f"{workflow.info().workflow_id}_child"
                )
            )
        else:
            self._task = await workflow.start_child_workflow(
                LongSleepWorkflow.run, id=f"{workflow.info().workflow_id}_child"
            )
        self._ready = True
        await self._task

    @workflow.query
    def ready(self) -> bool:
        return self._ready

    @workflow.signal
    async def cancel_child(self) -> None:
        self._task.cancel()


@pytest.mark.parametrize("use_execute", [True, False])
async def test_workflow_cancel_child_started(client: Client, use_execute: bool):
    async with new_worker(client, CancelChildWorkflow, LongSleepWorkflow) as worker:
        # Start workflow
        handle = await client.start_workflow(
            CancelChildWorkflow.run,
            use_execute,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Wait until child started
        async def child_started() -> bool:
            try:
                return await handle.query(
                    CancelChildWorkflow.ready
                ) and await client.get_workflow_handle_for(
                    LongSleepWorkflow.run,  # type: ignore[arg-type]
                    workflow_id=f"{handle.id}_child",
                ).query(LongSleepWorkflow.started)
            except RPCError as err:
                # Ignore not-found or failed precondition because child may
                # not have started yet
                if (
                    err.status == RPCStatusCode.NOT_FOUND
                    or err.status == RPCStatusCode.FAILED_PRECONDITION
                ):
                    return False
                raise

        await assert_eq_eventually(True, child_started)
        # Send cancel signal and wait on the handle
        await handle.signal(CancelChildWorkflow.cancel_child)
        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()
        assert isinstance(err.value.cause, ChildWorkflowError)
        assert isinstance(err.value.cause.cause, CancelledError)


@pytest.mark.skip(reason="unable to easily prevent child start currently")
async def test_workflow_cancel_child_unstarted(client: Client):
    raise NotImplementedError


@workflow.defn
class ReturnSignalWorkflow:
    def __init__(self) -> None:
        self._signal: Optional[str] = None

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self._signal is not None)
        assert self._signal
        return self._signal

    @workflow.signal
    def my_signal(self, value: str) -> None:
        self._signal = value


@workflow.defn
class SignalChildWorkflow:
    @workflow.run
    async def run(self, signal_value: str) -> str:
        handle = await workflow.start_child_workflow(
            ReturnSignalWorkflow.run, id=workflow.info().workflow_id + "_child"
        )
        await handle.signal(ReturnSignalWorkflow.my_signal, signal_value)
        return await handle


async def test_workflow_signal_child(client: Client):
    async with new_worker(client, SignalChildWorkflow, ReturnSignalWorkflow) as worker:
        result = await client.execute_workflow(
            SignalChildWorkflow.run,
            "some value",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result == "some value"


@workflow.defn
class CancelExternalWorkflow:
    @workflow.run
    async def run(self, external_workflow_id: str) -> None:
        await workflow.get_external_workflow_handle(external_workflow_id).cancel()


async def test_workflow_cancel_external(client: Client):
    async with new_worker(client, CancelExternalWorkflow, LongSleepWorkflow) as worker:
        # Start long sleep, then cancel and check that it got cancelled
        long_sleep_handle = await client.start_workflow(
            LongSleepWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await client.execute_workflow(
            CancelExternalWorkflow.run,
            long_sleep_handle.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        with pytest.raises(WorkflowFailureError) as err:
            await long_sleep_handle.result()
        assert isinstance(err.value.cause, CancelledError)


@dataclass
class SignalExternalWorkflowArgs:
    external_workflow_id: str
    signal_value: str


@workflow.defn
class SignalExternalWorkflow:
    @workflow.run
    async def run(self, args: SignalExternalWorkflowArgs) -> None:
        handle: workflow.ExternalWorkflowHandle[ReturnSignalWorkflow] = (
            workflow.get_external_workflow_handle_for(
                ReturnSignalWorkflow.run, args.external_workflow_id
            )
        )
        await handle.signal(ReturnSignalWorkflow.my_signal, args.signal_value)


async def test_workflow_signal_external(client: Client):
    async with new_worker(
        client, SignalExternalWorkflow, ReturnSignalWorkflow
    ) as worker:
        # Start return signal, then signal and check that it got signalled
        return_signal_handle = await client.start_workflow(
            ReturnSignalWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await client.execute_workflow(
            SignalExternalWorkflow.run,
            SignalExternalWorkflowArgs(
                external_workflow_id=return_signal_handle.id, signal_value="some value"
            ),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert "some value" == await return_signal_handle.result()


@workflow.defn
class MultiCancelWorkflow:
    @workflow.run
    async def run(self) -> List[str]:
        events: List[str] = []

        async def timer():
            nonlocal events
            try:
                await asyncio.sleep(1)
                events.append("timer success")
            except asyncio.CancelledError:
                events.append("timer cancelled")

        async def activity():
            nonlocal events
            try:
                await workflow.execute_activity(
                    wait_cancel, schedule_to_close_timeout=timedelta(5)
                )
                events.append("activity success")
            except ActivityError as err:
                if isinstance(err.cause, CancelledError):
                    events.append("activity cancelled")

        async def child(id: str):
            nonlocal events
            try:
                await workflow.execute_child_workflow(LongSleepWorkflow.run, id=id)
                events.append("child success")
            except ChildWorkflowError as err:
                if isinstance(err.cause, CancelledError):
                    events.append("child cancelled")

        # Start all tasks, send a cancel to all, and wait until done
        fut = asyncio.gather(
            timer(),
            asyncio.shield(timer()),
            activity(),
            child(f"child-{workflow.info().workflow_id}"),
            return_exceptions=True,
        )
        await asyncio.sleep(0.1)
        fut.cancel()
        await workflow.wait_condition(lambda: len(events) == 4, timeout=30)
        # Wait on the future just to make asyncio happy
        try:
            await fut
        except asyncio.CancelledError:
            pass
        return events


async def test_workflow_cancel_multi(client: Client):
    async with new_worker(
        client, MultiCancelWorkflow, LongSleepWorkflow, activities=[wait_cancel]
    ) as worker:
        results = await client.execute_workflow(
            MultiCancelWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert sorted(results) == [
            "activity cancelled",
            "child cancelled",
            "timer cancelled",
            "timer success",
        ]


@workflow.defn
class CancelUnsentWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Timer
        def raise_error():
            raise RuntimeError("should not get here")

        timer_handle = asyncio.get_running_loop().call_later(1, raise_error)
        timer_handle.cancel()

        async def wait_timer():
            await timer_handle  # type: ignore[misc]

        await self.wait_and_swallow(wait_timer())

        # Start activity
        activity_handle = workflow.start_activity(
            wait_cancel, schedule_to_close_timeout=timedelta(seconds=5)
        )
        activity_handle.cancel()
        await self.wait_and_swallow(activity_handle)

        # Execute activity
        activity_task = asyncio.create_task(
            workflow.execute_activity(
                wait_cancel, schedule_to_close_timeout=timedelta(seconds=5)
            )
        )
        activity_task.cancel()
        await self.wait_and_swallow(activity_task)

        # Start local activity
        activity_handle = workflow.start_local_activity(
            wait_cancel, schedule_to_close_timeout=timedelta(seconds=5)
        )
        activity_handle.cancel()
        await self.wait_and_swallow(activity_handle)

        # Execute local activity
        activity_task = asyncio.create_task(
            workflow.execute_local_activity(
                wait_cancel, schedule_to_close_timeout=timedelta(seconds=5)
            )
        )
        activity_task.cancel()
        await self.wait_and_swallow(activity_task)

        # Start child
        child_task1 = asyncio.create_task(
            workflow.start_child_workflow(
                LongSleepWorkflow.run, id=f"{workflow.info().workflow_id}_child1"
            )
        )
        child_task1.cancel()
        await self.wait_and_swallow(child_task1)

        # Execute child
        child_task2 = asyncio.create_task(
            workflow.execute_child_workflow(
                LongSleepWorkflow.run, id=f"{workflow.info().workflow_id}_child2"
            )
        )
        child_task2.cancel()
        await self.wait_and_swallow(child_task2)

        # Sleep for a short bit to force another task to run so we know that
        # workflow completion isn't saving us here
        await asyncio.sleep(0.01)

    async def wait_and_swallow(self, aw: Awaitable) -> None:
        try:
            await aw
        except (Exception, asyncio.CancelledError):
            pass


async def test_workflow_cancel_unsent(client: Client):
    workflow_id = f"workflow-{uuid.uuid4()}"
    async with new_worker(
        client, CancelUnsentWorkflow, LongSleepWorkflow, activities=[wait_cancel]
    ) as worker:
        await client.execute_workflow(
            CancelUnsentWorkflow.run,
            id=workflow_id,
            task_queue=worker.task_queue,
        )
    # Check history
    resp = await client.workflow_service.get_workflow_execution_history(
        GetWorkflowExecutionHistoryRequest(
            namespace=client.namespace,
            execution=WorkflowExecution(workflow_id=workflow_id),
        )
    )
    found_timer = False
    for event in resp.history.events:
        # No activities or children scheduled
        assert event.event_type is not EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        assert (
            event.event_type
            is not EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
        )
        # Make sure timer is just our 0.01 timer
        if event.event_type is EventType.EVENT_TYPE_TIMER_STARTED:
            assert (
                event.timer_started_event_attributes.start_to_fire_timeout.ToMilliseconds()
                == 10
            )
            found_timer = True
    assert found_timer


@workflow.defn
class ActivityTimeoutWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            wait_cancel,
            start_to_close_timeout=timedelta(milliseconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


async def test_workflow_activity_timeout(client: Client):
    async with new_worker(
        client, ActivityTimeoutWorkflow, activities=[wait_cancel]
    ) as worker:
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                ActivityTimeoutWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert isinstance(err.value.cause, ActivityError)
        assert isinstance(err.value.cause.cause, TimeoutError)


# Just serializes in a "payloads" wrapper
class SimpleCodec(PayloadCodec):
    async def encode(self, payloads: Sequence[Payload]) -> List[Payload]:
        wrapper = Payloads(payloads=payloads)
        return [
            Payload(
                metadata={"simple-codec": b"true"}, data=wrapper.SerializeToString()
            )
        ]

    async def decode(self, payloads: Sequence[Payload]) -> List[Payload]:
        payloads = list(payloads)
        if len(payloads) != 1:
            raise RuntimeError("Expected only a single payload")
        elif payloads[0].metadata.get("simple-codec") != b"true":
            raise RuntimeError("Not encoded with this codec")
        wrapper = Payloads()
        wrapper.ParseFromString(payloads[0].data)
        return list(wrapper.payloads)


async def test_workflow_with_codec(client: Client, env: WorkflowEnvironment):
    # Make client with this codec and run a couple of existing tests
    config = client.config()
    config["data_converter"] = DataConverter(payload_codec=SimpleCodec())
    client = Client(**config)
    await test_workflow_signal_and_query(client)
    await test_workflow_signal_and_query_errors(client)
    await test_workflow_simple_activity(client)
    await test_workflow_update_handlers_happy(client, env)


class PassThroughCodec(PayloadCodec):
    async def encode(self, payloads: Sequence[Payload]) -> List[Payload]:
        return list(payloads)

    async def decode(self, payloads: Sequence[Payload]) -> List[Payload]:
        return list(payloads)


async def test_workflow_with_passthrough_codec(client: Client):
    # Make client with this codec and run the activity test. This used to fail
    # because there was a bug where the codec couldn't reuse the passed-in
    # payloads.
    config = client.config()
    config["data_converter"] = DataConverter(payload_codec=PassThroughCodec())
    client = Client(**config)
    await test_workflow_simple_activity(client)


class CustomWorkflowRunner(WorkflowRunner):
    def __init__(self) -> None:
        super().__init__()
        self._unsandboxed = UnsandboxedWorkflowRunner()
        self._pairs: List[Tuple[WorkflowActivation, WorkflowActivationCompletion]] = []

    def prepare_workflow(self, defn: workflow._Definition) -> None:
        pass

    def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        return CustomWorkflowInstance(self, self._unsandboxed.create_instance(det))


class CustomWorkflowInstance(WorkflowInstance):
    def __init__(
        self, runner: CustomWorkflowRunner, unsandboxed: WorkflowInstance
    ) -> None:
        super().__init__()
        self._runner = runner
        self._unsandboxed = unsandboxed

    def activate(self, act: WorkflowActivation) -> WorkflowActivationCompletion:
        comp = self._unsandboxed.activate(act)
        self._runner._pairs.append((act, comp))
        return comp


async def test_workflow_with_custom_runner(client: Client):
    runner = CustomWorkflowRunner()
    async with new_worker(client, HelloWorkflow, workflow_runner=runner) as worker:
        result = await client.execute_workflow(
            HelloWorkflow.run,
            "Temporal",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result == "Hello, Temporal!"
    # Confirm first activation and last non-eviction-reply completion
    assert (
        runner._pairs[0][0].jobs[0].initialize_workflow.workflow_type == "HelloWorkflow"
    )
    assert (
        runner._pairs[-2][-1]
        .successful.commands[0]
        .complete_workflow_execution.result.data
        == b'"Hello, Temporal!"'
    )


@workflow.defn
class ContinueAsNewWorkflow:
    @workflow.run
    async def run(self, past_run_ids: List[str]) -> List[str]:
        # Check memo and retry policy
        assert workflow.memo_value("past_run_id_count") == len(past_run_ids)
        retry_policy = workflow.info().retry_policy
        assert retry_policy and retry_policy.maximum_attempts == 1000 + len(
            past_run_ids
        )

        if len(past_run_ids) == 5:
            return past_run_ids
        info = workflow.info()
        if info.continued_run_id:
            past_run_ids.append(info.continued_run_id)
        workflow.continue_as_new(
            past_run_ids,
            # Add memo and retry policy to check
            memo={"past_run_id_count": len(past_run_ids)},
            retry_policy=RetryPolicy(maximum_attempts=1000 + len(past_run_ids)),
        )


async def test_workflow_continue_as_new(client: Client, env: WorkflowEnvironment):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1424"
        )
    async with new_worker(client, ContinueAsNewWorkflow) as worker:
        handle = await client.start_workflow(
            ContinueAsNewWorkflow.run,
            cast(List[str], []),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            memo={"past_run_id_count": 0},
            retry_policy=RetryPolicy(maximum_attempts=1000),
        )
        result = await handle.result()
        assert len(result) == 5
        assert result[0] == handle.first_execution_run_id


sa_prefix = "python_test_"


def search_attributes_to_serializable(
    attrs: Union[SearchAttributes, TypedSearchAttributes],
) -> Mapping[str, Any]:
    if isinstance(attrs, TypedSearchAttributes):
        return {
            p.key.name: str(p.value) for p in attrs if p.key.name.startswith(sa_prefix)
        }
    return {
        # Ignore ones without our prefix
        k: [str(v) if isinstance(v, datetime) else v for v in vals]
        for k, vals in attrs.items()
        if k.startswith(sa_prefix)
    }


@workflow.defn
class SearchAttributeWorkflow:
    text_attribute = SearchAttributeKey.for_text(f"{sa_prefix}text")
    keyword_attribute = SearchAttributeKey.for_keyword(f"{sa_prefix}keyword")
    keyword_list_attribute = SearchAttributeKey.for_keyword_list(
        f"{sa_prefix}keyword_list"
    )
    int_attribute = SearchAttributeKey.for_int(f"{sa_prefix}int")
    float_attribute = SearchAttributeKey.for_float(f"{sa_prefix}double")
    bool_attribute = SearchAttributeKey.for_bool(f"{sa_prefix}bool")
    datetime_attribute = SearchAttributeKey.for_datetime(f"{sa_prefix}datetime")

    @workflow.run
    async def run(self) -> None:
        # Wait forever
        await asyncio.Future()

    @workflow.query
    def get_search_attributes_untyped(self) -> Mapping[str, Any]:
        return search_attributes_to_serializable(workflow.info().search_attributes)

    @workflow.query
    def get_search_attributes_typed(self) -> Mapping[str, Any]:
        return search_attributes_to_serializable(
            workflow.info().typed_search_attributes
        )

    @workflow.signal
    def do_search_attribute_update_untyped(self) -> None:
        empty_float_list: List[float] = []
        workflow.upsert_search_attributes(
            {
                SearchAttributeWorkflow.text_attribute.name: ["text2"],
                # We intentionally leave keyword off to confirm it still comes
                # back but replace keyword list
                SearchAttributeWorkflow.keyword_list_attribute.name: [
                    "keywordlist3",
                    "keywordlist4",
                ],
                SearchAttributeWorkflow.int_attribute.name: [456],
                # Empty list to confirm removed
                SearchAttributeWorkflow.float_attribute.name: empty_float_list,
                SearchAttributeWorkflow.bool_attribute.name: [False],
                SearchAttributeWorkflow.datetime_attribute.name: [
                    datetime(2003, 4, 5, 6, 7, 8, tzinfo=timezone(timedelta(hours=9)))
                ],
            }
        )

    @workflow.signal
    def do_search_attribute_update_typed(self) -> None:
        # Matches do_search_attribute_update_untyped
        workflow.upsert_search_attributes(
            [
                SearchAttributeWorkflow.text_attribute.value_set("text2"),
                SearchAttributeWorkflow.keyword_list_attribute.value_set(
                    ["keywordlist3", "keywordlist4"]
                ),
                SearchAttributeWorkflow.int_attribute.value_set(456),
                SearchAttributeWorkflow.float_attribute.value_unset(),
                SearchAttributeWorkflow.bool_attribute.value_set(False),
                SearchAttributeWorkflow.datetime_attribute.value_set(
                    datetime(2003, 4, 5, 6, 7, 8, tzinfo=timezone(timedelta(hours=9)))
                ),
            ]
        )


async def test_workflow_search_attributes(client: Client, env_type: str):
    if env_type != "local":
        pytest.skip("Only testing search attributes on local which disables cache")
    await ensure_search_attributes_present(
        client,
        SearchAttributeWorkflow.text_attribute,
        SearchAttributeWorkflow.keyword_attribute,
        SearchAttributeWorkflow.keyword_list_attribute,
        SearchAttributeWorkflow.int_attribute,
        SearchAttributeWorkflow.float_attribute,
        SearchAttributeWorkflow.bool_attribute,
        SearchAttributeWorkflow.datetime_attribute,
    )

    initial_attrs_untyped: SearchAttributes = {
        SearchAttributeWorkflow.text_attribute.name: ["text1"],
        SearchAttributeWorkflow.keyword_attribute.name: ["keyword1"],
        SearchAttributeWorkflow.keyword_list_attribute.name: [
            "keywordlist1",
            "keywordlist2",
        ],
        SearchAttributeWorkflow.int_attribute.name: [123],
        SearchAttributeWorkflow.float_attribute.name: [456.78],
        SearchAttributeWorkflow.bool_attribute.name: [True],
        SearchAttributeWorkflow.datetime_attribute.name: [
            datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        ],
    }
    initial_attrs_typed = TypedSearchAttributes(
        [
            SearchAttributePair(SearchAttributeWorkflow.text_attribute, "text1"),
            SearchAttributePair(SearchAttributeWorkflow.keyword_attribute, "keyword1"),
            SearchAttributePair(
                SearchAttributeWorkflow.keyword_list_attribute,
                ["keywordlist1", "keywordlist2"],
            ),
            SearchAttributePair(SearchAttributeWorkflow.int_attribute, 123),
            SearchAttributePair(SearchAttributeWorkflow.float_attribute, 456.78),
            SearchAttributePair(SearchAttributeWorkflow.bool_attribute, True),
            SearchAttributePair(
                SearchAttributeWorkflow.datetime_attribute,
                datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
            ),
        ]
    )
    updated_attrs_untyped: Dict[str, SearchAttributeValues] = {
        SearchAttributeWorkflow.text_attribute.name: ["text2"],
        SearchAttributeWorkflow.keyword_attribute.name: ["keyword1"],
        SearchAttributeWorkflow.keyword_list_attribute.name: [
            "keywordlist3",
            "keywordlist4",
        ],
        SearchAttributeWorkflow.int_attribute.name: [456],
        SearchAttributeWorkflow.float_attribute.name: cast(List[float], []),
        SearchAttributeWorkflow.bool_attribute.name: [False],
        SearchAttributeWorkflow.datetime_attribute.name: [
            datetime(2003, 4, 5, 6, 7, 8, tzinfo=timezone(timedelta(hours=9)))
        ],
    }
    updated_attrs_untyped_from_server: Dict[str, SearchAttributeValues] = {
        SearchAttributeWorkflow.text_attribute.name: ["text2"],
        SearchAttributeWorkflow.keyword_attribute.name: ["keyword1"],
        SearchAttributeWorkflow.keyword_list_attribute.name: [
            "keywordlist3",
            "keywordlist4",
        ],
        SearchAttributeWorkflow.int_attribute.name: [456],
        # No float value
        SearchAttributeWorkflow.bool_attribute.name: [False],
        SearchAttributeWorkflow.datetime_attribute.name: [
            datetime(2003, 4, 5, 6, 7, 8, tzinfo=timezone(timedelta(hours=9)))
        ],
    }
    updated_attrs_typed = TypedSearchAttributes(
        [
            SearchAttributePair(SearchAttributeWorkflow.text_attribute, "text2"),
            SearchAttributePair(SearchAttributeWorkflow.keyword_attribute, "keyword1"),
            SearchAttributePair(
                SearchAttributeWorkflow.keyword_list_attribute,
                ["keywordlist3", "keywordlist4"],
            ),
            SearchAttributePair(SearchAttributeWorkflow.int_attribute, 456),
            SearchAttributePair(SearchAttributeWorkflow.bool_attribute, False),
            SearchAttributePair(
                SearchAttributeWorkflow.datetime_attribute,
                datetime(2003, 4, 5, 6, 7, 8, tzinfo=timezone(timedelta(hours=9))),
            ),
        ]
    )

    async def describe_attributes_untyped(handle: WorkflowHandle) -> SearchAttributes:
        # Remove any not our prefix
        return {
            k: v
            for k, v in (await handle.describe()).search_attributes.items()
            if k.startswith(sa_prefix)
        }

    async def describe_attributes_typed(
        handle: WorkflowHandle,
    ) -> TypedSearchAttributes:
        # Remove any not our prefix
        attrs = (await handle.describe()).typed_search_attributes
        return dataclasses.replace(
            attrs,
            search_attributes=[p for p in attrs if p.key.name.startswith(sa_prefix)],
        )

    # Mutate with untyped mutators (but check untyped/typed)
    async with new_worker(client, SearchAttributeWorkflow) as worker:
        handle = await client.start_workflow(
            SearchAttributeWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            search_attributes=initial_attrs_untyped,
        )

        # Check query/describe
        assert search_attributes_to_serializable(
            initial_attrs_untyped
        ) == await handle.query(SearchAttributeWorkflow.get_search_attributes_untyped)
        assert initial_attrs_untyped == await describe_attributes_untyped(handle)
        assert search_attributes_to_serializable(
            initial_attrs_typed
        ) == await handle.query(SearchAttributeWorkflow.get_search_attributes_typed)
        assert initial_attrs_typed == await describe_attributes_typed(handle)

        # Update and check query/describe
        await handle.signal(SearchAttributeWorkflow.do_search_attribute_update_untyped)
        assert search_attributes_to_serializable(
            updated_attrs_untyped
        ) == await handle.query(SearchAttributeWorkflow.get_search_attributes_untyped)
        assert updated_attrs_untyped_from_server == await describe_attributes_untyped(
            handle
        )
        assert search_attributes_to_serializable(
            updated_attrs_typed
        ) == await handle.query(SearchAttributeWorkflow.get_search_attributes_typed)
        assert updated_attrs_typed == await describe_attributes_typed(handle)

    # Mutate with typed mutators (but check untyped/typed)
    async with new_worker(client, SearchAttributeWorkflow) as worker:
        handle = await client.start_workflow(
            SearchAttributeWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            search_attributes=initial_attrs_typed,
        )

        # Check query/describe
        assert search_attributes_to_serializable(
            initial_attrs_untyped
        ) == await handle.query(SearchAttributeWorkflow.get_search_attributes_untyped)
        assert initial_attrs_untyped == await describe_attributes_untyped(handle)
        assert search_attributes_to_serializable(
            initial_attrs_typed
        ) == await handle.query(SearchAttributeWorkflow.get_search_attributes_typed)
        assert initial_attrs_typed == await describe_attributes_typed(handle)

        # Update and check query/describe
        await handle.signal(SearchAttributeWorkflow.do_search_attribute_update_typed)
        assert search_attributes_to_serializable(
            updated_attrs_untyped
        ) == await handle.query(SearchAttributeWorkflow.get_search_attributes_untyped)
        assert updated_attrs_untyped_from_server == await describe_attributes_untyped(
            handle
        )
        assert search_attributes_to_serializable(
            updated_attrs_typed
        ) == await handle.query(SearchAttributeWorkflow.get_search_attributes_typed)
        assert updated_attrs_typed == await describe_attributes_typed(handle)


@workflow.defn
class NoSearchAttributesWorkflow:
    @workflow.run
    async def run(self) -> None:
        workflow.upsert_search_attributes(
            [
                SearchAttributeWorkflow.text_attribute.value_set("text2"),
            ]
        )
        # All we need to do is complete


async def test_workflow_no_initial_search_attributes(client: Client, env_type: str):
    if env_type != "local":
        pytest.skip("Only testing search attributes on local which disables cache")
    await ensure_search_attributes_present(
        client,
        SearchAttributeWorkflow.text_attribute,
    )
    async with new_worker(client, NoSearchAttributesWorkflow) as worker:
        handle = await client.start_workflow(
            NoSearchAttributesWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            # importantly, no initial search attributes
        )
        await handle.result()


@workflow.defn
class LoggingWorkflow:
    def __init__(self) -> None:
        self._last_signal = "<none>"

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._last_signal == "finish")

    @workflow.signal
    def my_signal(self, value: str) -> None:
        self._last_signal = value
        workflow.logger.info(f"Signal: {value}")

    @workflow.update
    def my_update(self, value: str) -> None:
        workflow.logger.info(f"Update: {value}")

    @workflow.query
    def last_signal(self) -> str:
        return self._last_signal


class LogCapturer:
    def __init__(self) -> None:
        self.log_queue: queue.Queue[logging.LogRecord] = queue.Queue()

    @contextmanager
    def logs_captured(self, *loggers: logging.Logger):
        handler = logging.handlers.QueueHandler(self.log_queue)

        prev_levels = [l.level for l in loggers]
        for l in loggers:
            l.setLevel(logging.INFO)
            l.addHandler(handler)
        try:
            yield self
        finally:
            for i, l in enumerate(loggers):
                l.removeHandler(handler)
                l.setLevel(prev_levels[i])

    def find_log(self, starts_with: str) -> Optional[logging.LogRecord]:
        for record in cast(List[logging.LogRecord], self.log_queue.queue):
            if record.message.startswith(starts_with):
                return record
        return None


async def test_workflow_logging(client: Client, env: WorkflowEnvironment):
    workflow.logger.full_workflow_info_on_extra = True
    with LogCapturer().logs_captured(
        workflow.logger.base_logger, activity.logger.base_logger
    ) as capturer:
        # Log two signals and kill worker before completing. Need to disable
        # workflow cache since we restart the worker and don't want to pay the
        # sticky queue penalty.
        async with new_worker(
            client, LoggingWorkflow, max_cached_workflows=0
        ) as worker:
            handle = await client.start_workflow(
                LoggingWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            # Send some signals and updates
            await handle.signal(LoggingWorkflow.my_signal, "signal 1")
            await handle.signal(LoggingWorkflow.my_signal, "signal 2")
            await handle.execute_update(
                LoggingWorkflow.my_update, "update 1", id="update-1"
            )
            await handle.execute_update(
                LoggingWorkflow.my_update, "update 2", id="update-2"
            )
            assert "signal 2" == await handle.query(LoggingWorkflow.last_signal)

        # Confirm logs were produced
        assert capturer.find_log("Signal: signal 1 ({'attempt':")
        assert capturer.find_log("Signal: signal 2")
        assert capturer.find_log("Update: update 1")
        assert capturer.find_log("Update: update 2")
        assert not capturer.find_log("Signal: signal 3")
        # Also make sure it has some workflow info and correct funcName
        record = capturer.find_log("Signal: signal 1")
        assert (
            record
            and record.__dict__["temporal_workflow"]["workflow_type"]
            == "LoggingWorkflow"
            and record.funcName == "my_signal"
        )
        # Since we enabled full info, make sure it's there
        assert isinstance(record.__dict__["workflow_info"], workflow.Info)
        # Check the log emitted by the update execution.
        record = capturer.find_log("Update: update 1")
        assert (
            record
            and record.__dict__["temporal_workflow"]["update_id"] == "update-1"
            and record.__dict__["temporal_workflow"]["update_name"] == "my_update"
            and "'update_id': 'update-1'" in record.message
            and "'update_name': 'my_update'" in record.message
        )

        # Clear queue and start a new one with more signals
        capturer.log_queue.queue.clear()
        async with new_worker(
            client,
            LoggingWorkflow,
            task_queue=worker.task_queue,
            max_cached_workflows=0,
        ) as worker:
            # Send signals and updates
            await handle.signal(LoggingWorkflow.my_signal, "signal 3")
            await handle.signal(LoggingWorkflow.my_signal, "finish")
            await handle.result()

        # Confirm replayed logs are not present but new ones are
        assert not capturer.find_log("Signal: signal 1")
        assert not capturer.find_log("Signal: signal 2")
        assert capturer.find_log("Signal: signal 3")
        assert capturer.find_log("Signal: finish")


@activity.defn
async def task_fail_once_activity() -> None:
    if activity.info().attempt == 1:
        raise RuntimeError("Intentional activity task failure")


task_fail_once_workflow_has_failed = False


@workflow.defn(sandboxed=False)
class TaskFailOnceWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Fail on first attempt
        global task_fail_once_workflow_has_failed
        if not task_fail_once_workflow_has_failed:
            task_fail_once_workflow_has_failed = True
            raise RuntimeError("Intentional workflow task failure")

        # Execute activity that will fail once
        await workflow.execute_activity(
            task_fail_once_activity,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=1),
                backoff_coefficient=1.0,
                maximum_attempts=2,
            ),
        )


async def test_workflow_logging_task_fail(client: Client):
    with LogCapturer().logs_captured(
        activity.logger.base_logger, temporalio.worker._workflow_instance.logger
    ) as capturer:
        async with new_worker(
            client, TaskFailOnceWorkflow, activities=[task_fail_once_activity]
        ) as worker:
            await client.execute_workflow(
                TaskFailOnceWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

        wf_task_record = capturer.find_log("Failed activation on workflow")
        assert wf_task_record
        assert "Intentional workflow task failure" in wf_task_record.message
        assert (
            getattr(wf_task_record, "temporal_workflow")["workflow_type"]
            == "TaskFailOnceWorkflow"
        )

        act_task_record = capturer.find_log("Completing activity as failed")
        assert act_task_record
        assert "Intentional activity task failure" in act_task_record.message
        assert (
            getattr(act_task_record, "temporal_activity")["activity_type"]
            == "task_fail_once_activity"
        )


@workflow.defn
class StackTraceWorkflow:
    def __init__(self) -> None:
        self._status = "created"

    @workflow.run
    async def run(self) -> None:
        # Start several tasks
        awaitables = [
            asyncio.sleep(1000),
            workflow.execute_activity(
                wait_cancel, schedule_to_close_timeout=timedelta(seconds=1000)
            ),
            workflow.execute_child_workflow(
                LongSleepWorkflow.run, id=f"{workflow.info().workflow_id}_child"
            ),
            self.never_completing_coroutine(),
        ]
        await workflow.wait([asyncio.create_task(v) for v in awaitables])

    async def never_completing_coroutine(self) -> None:
        self._status = "waiting"
        await workflow.wait_condition(lambda: False)

    @workflow.query
    def status(self) -> str:
        return self._status


async def test_workflow_stack_trace(client: Client):
    async with new_worker(
        client, StackTraceWorkflow, LongSleepWorkflow, activities=[wait_cancel]
    ) as worker:
        handle = await client.start_workflow(
            StackTraceWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Wait until waiting
        async def status() -> str:
            return await handle.query(StackTraceWorkflow.status)

        await assert_eq_eventually("waiting", status)

        # Send stack trace query
        trace = await handle.query("__stack_trace")
        # TODO(cretz): Do more specific checks once we clean up traces
        assert "never_completing_coroutine" in trace


async def test_workflow_enhanced_stack_trace(client: Client):
    """Expected format of __enhanced_stack_trace:

    EnhancedStackTrace : {

        sdk (StackTraceSDKInfo) : {
            name: string,
            version: string
        },

        sources (map<string, StackTraceFileSlice>) : {
            filename: (StackTraceFileSlice) {
                line_offset: int,
                content: string
            },
            ...
        },

        stacks (StackTrace[]) : [
            (StackTraceFileLocation) {
                file_path: string,
                line: int,
                column: int,
                function_name: string,
                internal_code: bool
            },
            ...
        ]
    }

    More details available in API repository: temporal/api/sdk/v1/enhanced_stack_trace.proto
    """

    async with new_worker(
        client, StackTraceWorkflow, LongSleepWorkflow, activities=[wait_cancel]
    ) as worker:
        handle = await client.start_workflow(
            StackTraceWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Wait until waiting
        async def status() -> str:
            return await handle.query(StackTraceWorkflow.status)

        await assert_eq_eventually("waiting", status)

        # Send stack trace query
        trace = await handle.query("__enhanced_stack_trace")

        assert type(trace) == EnhancedStackTrace

        assert "never_completing_coroutine" in [
            loc.function_name for stack in trace.stacks for loc in stack.locations
        ]

        # first line of never_completing_coroutine
        cur_source = None
        for source in trace.sources.keys():
            if source.endswith("test_workflow.py"):
                cur_source = source

        # make sure the source exists
        assert cur_source is not None

        # make sure the line is present in the source
        assert 'self._status = "waiting"' in trace.sources[cur_source].content
        assert trace.sdk.version == __version__


async def test_workflow_external_enhanced_stack_trace(client: Client):
    async with new_worker(
        client,
        ExternalStackTraceWorkflow,
        activities=[external_wait_cancel],
    ) as worker:
        handle = await client.start_workflow(
            ExternalStackTraceWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        async def status() -> str:
            return await handle.query(ExternalStackTraceWorkflow.status)

        await assert_eq_eventually("waiting", status)

        trace = await handle.query("__enhanced_stack_trace")

        # test that a coroutine only has the source as its stack

        assert type(trace) == EnhancedStackTrace

        assert "never_completing_coroutine" in [
            loc.function_name for stack in trace.stacks for loc in stack.locations
        ]

        fn = None
        for source in trace.sources.keys():
            if source.endswith("external_coroutine.py"):
                fn = source

        assert fn is not None
        assert (
            'status[0] = "waiting"  # external coroutine test'
            in trace.sources[fn].content
        )
        assert trace.sdk.version == __version__


@dataclass
class MyDataClass:
    field1: str

    def assert_expected(self) -> None:
        # Part of the assertion is that this is the right type, which is
        # confirmed just by calling the method. We also check the field.
        assert self.field1 == "some value"


@activity.defn
async def data_class_typed_activity(param: MyDataClass) -> MyDataClass:
    param.assert_expected()
    return param


@runtime_checkable
@workflow.defn(name="DataClassTypedWorkflow")
class DataClassTypedWorkflowProto(Protocol):
    @workflow.run
    async def run(self, arg: MyDataClass) -> MyDataClass: ...

    @workflow.signal
    def signal_sync(self, param: MyDataClass) -> None: ...

    @workflow.query
    def query_sync(self, param: MyDataClass) -> MyDataClass: ...

    @workflow.signal
    def complete(self) -> None: ...


@workflow.defn(name="DataClassTypedWorkflow")
class DataClassTypedWorkflowAbstract(ABC):
    @workflow.run
    @abstractmethod
    async def run(self, arg: MyDataClass) -> MyDataClass: ...

    @workflow.signal
    @abstractmethod
    def signal_sync(self, param: MyDataClass) -> None: ...

    @workflow.query
    @abstractmethod
    def query_sync(self, param: MyDataClass) -> MyDataClass: ...

    @workflow.signal
    @abstractmethod
    def complete(self) -> None: ...


@workflow.defn
class DataClassTypedWorkflow(DataClassTypedWorkflowAbstract):
    def __init__(self) -> None:
        self._should_complete = asyncio.Event()

    @workflow.run
    async def run(self, param: MyDataClass) -> MyDataClass:
        param.assert_expected()
        # Only do activities and child workflows on top level
        if not workflow.info().parent:
            param = await workflow.execute_activity(
                data_class_typed_activity,
                param,
                start_to_close_timeout=timedelta(seconds=30),
            )
            param.assert_expected()
            param = await workflow.execute_local_activity(
                data_class_typed_activity,
                param,
                start_to_close_timeout=timedelta(seconds=30),
            )
            param.assert_expected()
            child_handle = await workflow.start_child_workflow(
                DataClassTypedWorkflow.run,
                param,
                id=f"{workflow.info().workflow_id}_child",
            )
            await child_handle.signal(DataClassTypedWorkflow.signal_sync, param)
            await child_handle.signal(DataClassTypedWorkflow.signal_async, param)
            await child_handle.signal(DataClassTypedWorkflow.complete)
            param = await child_handle
            param.assert_expected()
        await self._should_complete.wait()
        return param

    @workflow.signal
    def signal_sync(self, param: MyDataClass) -> None:
        param.assert_expected()

    @workflow.signal
    async def signal_async(self, param: MyDataClass) -> None:
        param.assert_expected()

    @workflow.query
    def query_sync(self, param: MyDataClass) -> MyDataClass:
        param.assert_expected()
        return param

    @workflow.query
    async def query_async(self, param: MyDataClass) -> MyDataClass:
        return param

    @workflow.signal
    def complete(self) -> None:
        self._should_complete.set()


async def test_workflow_dataclass_typed(client: Client, env: WorkflowEnvironment):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-core/issues/390"
        )
    async with new_worker(
        client, DataClassTypedWorkflow, activities=[data_class_typed_activity]
    ) as worker:
        val = MyDataClass(field1="some value")
        handle = await client.start_workflow(
            DataClassTypedWorkflow.run,
            val,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(DataClassTypedWorkflow.signal_sync, val)
        await handle.signal(DataClassTypedWorkflow.signal_async, val)
        (await handle.query(DataClassTypedWorkflow.query_sync, val)).assert_expected()
        # TODO(cretz): Why does MyPy need this annotated?
        query_result: MyDataClass = await handle.query(
            DataClassTypedWorkflow.query_async, val
        )
        query_result.assert_expected()
        await handle.signal(DataClassTypedWorkflow.complete)
        (await handle.result()).assert_expected()


async def test_workflow_separate_protocol(client: Client):
    # This test is to confirm that protocols can be used as "interfaces" for
    # when the workflow impl is absent
    async with new_worker(
        client, DataClassTypedWorkflow, activities=[data_class_typed_activity]
    ) as worker:
        assert isinstance(DataClassTypedWorkflow(), DataClassTypedWorkflowProto)
        val = MyDataClass(field1="some value")
        handle = await client.start_workflow(
            DataClassTypedWorkflowProto.run,
            val,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(DataClassTypedWorkflowProto.signal_sync, val)
        (
            await handle.query(DataClassTypedWorkflowProto.query_sync, val)
        ).assert_expected()
        await handle.signal(DataClassTypedWorkflowProto.complete)
        (await handle.result()).assert_expected()


async def test_workflow_separate_abstract(client: Client):
    # This test is to confirm that abstract classes can be used as "interfaces"
    # for when the workflow impl is absent
    async with new_worker(
        client, DataClassTypedWorkflow, activities=[data_class_typed_activity]
    ) as worker:
        assert issubclass(DataClassTypedWorkflow, DataClassTypedWorkflowAbstract)
        val = MyDataClass(field1="some value")
        handle = await client.start_workflow(
            DataClassTypedWorkflowAbstract.run,
            val,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(DataClassTypedWorkflowAbstract.signal_sync, val)
        (
            await handle.query(DataClassTypedWorkflowAbstract.query_sync, val)
        ).assert_expected()
        await handle.signal(DataClassTypedWorkflowAbstract.complete)
        (await handle.result()).assert_expected()


async def test_workflow_already_started(client: Client, env: WorkflowEnvironment):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1220"
        )
    async with new_worker(client, LongSleepWorkflow) as worker:
        id = f"workflow-{uuid.uuid4()}"
        # Try to start it twice
        await client.start_workflow(
            LongSleepWorkflow.run,
            id=id,
            task_queue=worker.task_queue,
        )
        with pytest.raises(WorkflowAlreadyStartedError):
            await client.start_workflow(
                LongSleepWorkflow.run,
                id=id,
                task_queue=worker.task_queue,
            )


@workflow.defn
class ChildAlreadyStartedWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Try to start it twice
        id = f"{workflow.info().workflow_id}_child"
        await workflow.start_child_workflow(LongSleepWorkflow.run, id=id)
        try:
            await workflow.start_child_workflow(LongSleepWorkflow.run, id=id)
        except WorkflowAlreadyStartedError:
            raise ApplicationError("Already started")


async def test_workflow_child_already_started(client: Client, env: WorkflowEnvironment):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1220"
        )
    async with new_worker(
        client, ChildAlreadyStartedWorkflow, LongSleepWorkflow
    ) as worker:
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                ChildAlreadyStartedWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert isinstance(err.value.cause, ApplicationError)
        assert err.value.cause.message == "Already started"


@workflow.defn
class TypedConfigWorkflow:
    @workflow.run
    async def run(self) -> None:
        retry_policy = RetryPolicy(initial_interval=timedelta(milliseconds=1))
        # Activity
        activity_config = workflow.ActivityConfig(
            retry_policy=retry_policy,
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        result = await workflow.execute_activity(
            fail_until_attempt_activity, 2, **activity_config
        )
        assert result == "attempt: 2"
        # Local activity
        local_activity_config = workflow.LocalActivityConfig(
            retry_policy=retry_policy,
            schedule_to_close_timeout=timedelta(seconds=5),
        )
        result = await workflow.execute_local_activity(
            fail_until_attempt_activity, 2, **local_activity_config
        )
        assert result == "attempt: 2"
        # Child workflow
        child_config = workflow.ChildWorkflowConfig(
            id=f"{workflow.info().workflow_id}_child",
            retry_policy=retry_policy,
        )
        result = await workflow.execute_child_workflow(
            FailUntilAttemptWorkflow.run, 2, **child_config
        )
        assert result == "attempt: 2"


async def test_workflow_typed_config(client: Client):
    async with new_worker(
        client,
        TypedConfigWorkflow,
        FailUntilAttemptWorkflow,
        activities=[fail_until_attempt_activity],
    ) as worker:
        await client.execute_workflow(
            TypedConfigWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


@activity.defn
async def fail_until_attempt_activity(until_attempt: int) -> str:
    if activity.info().attempt < until_attempt:
        raise ApplicationError("Attempt too low")
    return f"attempt: {activity.info().attempt}"


@workflow.defn
class FailUntilAttemptWorkflow:
    @workflow.run
    async def run(self, until_attempt: int) -> str:
        if workflow.info().attempt < until_attempt:
            raise ApplicationError("Attempt too low")
        return f"attempt: {workflow.info().attempt}"


@workflow.defn
class LocalActivityBackoffWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_local_activity(
            fail_until_attempt_activity,
            2,
            start_to_close_timeout=timedelta(minutes=1),
            local_retry_threshold=timedelta(seconds=1),
            retry_policy=RetryPolicy(
                maximum_attempts=2, initial_interval=timedelta(seconds=2)
            ),
        )


async def test_workflow_local_activity_backoff(client: Client):
    workflow_id = f"workflow-{uuid.uuid4()}"
    async with new_worker(
        client, LocalActivityBackoffWorkflow, activities=[fail_until_attempt_activity]
    ) as worker:
        await client.execute_workflow(
            LocalActivityBackoffWorkflow.run,
            id=workflow_id,
            task_queue=worker.task_queue,
            task_timeout=timedelta(seconds=3),
        )
    # Check history
    resp = await client.workflow_service.get_workflow_execution_history(
        GetWorkflowExecutionHistoryRequest(
            namespace=client.namespace,
            execution=WorkflowExecution(workflow_id=workflow_id),
        )
    )
    assert 1 == sum(
        1
        for e in resp.history.events
        if e.event_type is EventType.EVENT_TYPE_TIMER_FIRED
    )
    assert 2 == sum(
        1
        for e in resp.history.events
        if e.event_type is EventType.EVENT_TYPE_MARKER_RECORDED
    )


deadlock_thread_event = threading.Event()


# We cannot sandbox this because we are intentionally non-deterministic when we
# set the global threading event
@workflow.defn(sandboxed=False)
class DeadlockedWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Block on threading event
        deadlock_thread_event.wait()


async def test_workflow_deadlock(client: Client):
    # Disable safe eviction so the worker can complete
    async with new_worker(
        client, DeadlockedWorkflow, disable_safe_workflow_eviction=True
    ) as worker:
        if worker._workflow_worker:
            worker._workflow_worker._deadlock_timeout_seconds = 1
        deadlock_thread_event.clear()
        handle = await client.start_workflow(
            DeadlockedWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        async def last_history_task_failure() -> str:
            resp = await client.workflow_service.get_workflow_execution_history(
                GetWorkflowExecutionHistoryRequest(
                    namespace=client.namespace,
                    execution=WorkflowExecution(workflow_id=handle.id),
                ),
            )
            for event in reversed(resp.history.events):
                if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED:
                    return event.workflow_task_failed_event_attributes.failure.message
            return "<no failure>"

        try:
            await assert_eq_eventually(
                "[TMPRL1101] Potential deadlock detected: workflow didn't yield within 1 second(s).",
                last_history_task_failure,
                timeout=timedelta(seconds=5),
                interval=timedelta(seconds=1),
            )
        finally:
            deadlock_thread_event.set()


@workflow.defn
class EvictionDeadlockWorkflow:
    def __init__(self) -> None:
        self.val = 1

    async def wait_until_positive(self):
        while True:
            await workflow.wait_condition(lambda: self.val > 0)
            self.val = -self.val

    async def wait_until_negative(self):
        while True:
            await workflow.wait_condition(lambda: self.val < 0)
            self.val = -self.val

    @workflow.run
    async def run(self):
        await asyncio.gather(self.wait_until_negative(), self.wait_until_positive())


async def test_workflow_eviction_deadlock(client: Client):
    # We are running the worker, but we can't ever shut it down on eviction
    # error so we send shutdown in the background and leave this worker dangling
    worker = new_worker(client, EvictionDeadlockWorkflow)
    if worker._workflow_worker:
        worker._workflow_worker._deadlock_timeout_seconds = 1
    worker_task = asyncio.create_task(worker.run())

    # Run workflow that deadlocks
    handle = await client.start_workflow(
        EvictionDeadlockWorkflow.run,
        id=f"workflow-{uuid.uuid4()}",
        task_queue=worker.task_queue,
    )

    async def last_history_task_failure() -> str:
        resp = await client.workflow_service.get_workflow_execution_history(
            GetWorkflowExecutionHistoryRequest(
                namespace=client.namespace,
                execution=WorkflowExecution(workflow_id=handle.id),
            ),
        )
        for event in reversed(resp.history.events):
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED:
                return event.workflow_task_failed_event_attributes.failure.message
        return "<no failure>"

    await assert_eq_eventually(
        "[TMPRL1101] Potential deadlock detected: workflow didn't yield within 1 second(s).",
        last_history_task_failure,
        timeout=timedelta(seconds=5),
        interval=timedelta(seconds=1),
    )

    # Send cancel but don't wait
    worker_task.cancel()


class PatchWorkflowBase:
    def __init__(self) -> None:
        self._result = "<unset>"

    @workflow.query
    def result(self) -> str:
        return self._result


@workflow.defn(name="patch-workflow")
class PrePatchWorkflow(PatchWorkflowBase):
    @workflow.run
    async def run(self) -> None:
        self._result = "pre-patch"


@workflow.defn(name="patch-workflow")
class PatchWorkflow(PatchWorkflowBase):
    @workflow.run
    async def run(self) -> None:
        if workflow.patched("my-patch"):
            self._result = "post-patch"
        else:
            self._result = "pre-patch"


@workflow.defn(name="patch-workflow")
class DeprecatePatchWorkflow(PatchWorkflowBase):
    @workflow.run
    async def run(self) -> None:
        workflow.deprecate_patch("my-patch")
        self._result = "post-patch"


@workflow.defn(name="patch-workflow")
class PostPatchWorkflow(PatchWorkflowBase):
    @workflow.run
    async def run(self) -> None:
        self._result = "post-patch"


async def test_workflow_patch(client: Client):
    workflow_run = PrePatchWorkflow.run
    task_queue = str(uuid.uuid4())

    async def execute() -> WorkflowHandle:
        handle = await client.start_workflow(
            workflow_run, id=f"workflow-{uuid.uuid4()}", task_queue=task_queue
        )
        await handle.result()
        return handle

    async def query_result(handle: WorkflowHandle) -> str:
        return await handle.query(PatchWorkflowBase.result)

    # Run a simple pre-patch workflow. Need to disable workflow cache since we
    # restart the worker and don't want to pay the sticky queue penalty.
    async with new_worker(
        client, PrePatchWorkflow, task_queue=task_queue, max_cached_workflows=0
    ):
        pre_patch_handle = await execute()
        assert "pre-patch" == await query_result(pre_patch_handle)

    # Confirm patched workflow gives old result for pre-patched but new result
    # for patched
    async with new_worker(
        client, PatchWorkflow, task_queue=task_queue, max_cached_workflows=0
    ):
        patch_handle = await execute()
        assert "post-patch" == await query_result(patch_handle)
        assert "pre-patch" == await query_result(pre_patch_handle)

    # Confirm what works during deprecated
    async with new_worker(
        client, DeprecatePatchWorkflow, task_queue=task_queue, max_cached_workflows=0
    ):
        deprecate_patch_handle = await execute()
        assert "post-patch" == await query_result(deprecate_patch_handle)
        assert "post-patch" == await query_result(patch_handle)

    # Confirm what works when deprecation gone
    async with new_worker(
        client, PostPatchWorkflow, task_queue=task_queue, max_cached_workflows=0
    ):
        post_patch_handle = await execute()
        assert "post-patch" == await query_result(post_patch_handle)
        assert "post-patch" == await query_result(deprecate_patch_handle)
        # TODO(cretz): This causes a non-determinism failure due to having the
        # patch marker, but we don't have an easy way to test it
        # await query_result(patch_handle)


@workflow.defn(name="patch-memoized")
class PatchMemoizedWorkflowUnpatched:
    def __init__(self, *, should_patch: bool = False) -> None:
        self.should_patch = should_patch
        self._waiting_signal = True

    @workflow.run
    async def run(self) -> List[str]:
        results: List[str] = []
        if self.should_patch and workflow.patched("some-patch"):
            results.append("pre-patch")
        self._waiting_signal = True
        await workflow.wait_condition(lambda: not self._waiting_signal)
        results.append("some-value")
        if self.should_patch and workflow.patched("some-patch"):
            results.append("post-patch")
        return results

    @workflow.signal
    def signal(self) -> None:
        self._waiting_signal = False

    @workflow.query
    def waiting_signal(self) -> bool:
        return self._waiting_signal


@workflow.defn(name="patch-memoized")
class PatchMemoizedWorkflowPatched(PatchMemoizedWorkflowUnpatched):
    def __init__(self) -> None:
        super().__init__(should_patch=True)

    @workflow.run
    async def run(self) -> List[str]:
        return await super().run()


async def test_workflow_patch_memoized(client: Client):
    # Start a worker with the workflow unpatched and wait until halfway through.
    # Need to disable workflow cache since we restart the worker and don't want
    # to pay the sticky queue penalty.
    task_queue = f"tq-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[PatchMemoizedWorkflowUnpatched],
        max_cached_workflows=0,
    ):
        pre_patch_handle = await client.start_workflow(
            PatchMemoizedWorkflowUnpatched.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=task_queue,
        )

        # Need to wait until it has gotten halfway through
        async def waiting_signal() -> bool:
            return await pre_patch_handle.query(
                PatchMemoizedWorkflowUnpatched.waiting_signal
            )

        await assert_eq_eventually(True, waiting_signal)

    # Now start the worker again, but this time with a patched workflow
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[PatchMemoizedWorkflowPatched],
        max_cached_workflows=0,
    ):
        # Start a new workflow post patch
        post_patch_handle = await client.start_workflow(
            PatchMemoizedWorkflowPatched.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=task_queue,
        )

        # Send signal to both and check results
        await pre_patch_handle.signal(PatchMemoizedWorkflowPatched.signal)
        await post_patch_handle.signal(PatchMemoizedWorkflowPatched.signal)

        # Confirm expected values
        assert ["some-value"] == await pre_patch_handle.result()
        assert [
            "pre-patch",
            "some-value",
            "post-patch",
        ] == await post_patch_handle.result()


@workflow.defn
class UUIDWorkflow:
    def __init__(self) -> None:
        self._result = "<unset>"

    @workflow.run
    async def run(self) -> None:
        self._result = str(workflow.uuid4())

    @workflow.query
    def result(self) -> str:
        return self._result


async def test_workflow_uuid(client: Client):
    task_queue = str(uuid.uuid4())
    async with new_worker(
        client, UUIDWorkflow, task_queue=task_queue, max_cached_workflows=0
    ):
        # Get two handle UUID results. Need to disable workflow cache since we
        # restart the worker and don't want to pay the sticky queue penalty.
        handle1 = await client.start_workflow(
            UUIDWorkflow.run, id=f"workflow-{uuid.uuid4()}", task_queue=task_queue
        )
        await handle1.result()
        handle1_query_result = await handle1.query(UUIDWorkflow.result)

        handle2 = await client.start_workflow(
            UUIDWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        await handle2.result()
        handle2_query_result = await handle2.query(UUIDWorkflow.result)

        # Confirm they aren't equal to each other but they are equal to retries
        # of the same query
        assert handle1_query_result != handle2_query_result
        assert handle1_query_result == await handle1.query(UUIDWorkflow.result)
        assert handle2_query_result == await handle2.query(UUIDWorkflow.result)

    # Now confirm those results are the same even on a new worker
    async with new_worker(
        client, UUIDWorkflow, task_queue=task_queue, max_cached_workflows=0
    ):
        assert handle1_query_result == await handle1.query(UUIDWorkflow.result)
        assert handle2_query_result == await handle2.query(UUIDWorkflow.result)


@activity.defn(name="custom-name")
class CallableClassActivity:
    def __init__(self, orig_field1: str) -> None:
        self.orig_field1 = orig_field1

    async def __call__(self, to_add: MyDataClass) -> MyDataClass:
        return MyDataClass(field1=self.orig_field1 + to_add.field1)


@workflow.defn
class ActivityCallableClassWorkflow:
    @workflow.run
    async def run(self, to_add: MyDataClass) -> MyDataClass:
        result = await workflow.execute_activity_class(
            CallableClassActivity, to_add, start_to_close_timeout=timedelta(seconds=30)
        )
        assert isinstance(result, MyDataClass)
        return result


async def test_workflow_activity_callable_class(client: Client):
    activity_instance = CallableClassActivity("in worker")
    async with new_worker(
        client, ActivityCallableClassWorkflow, activities=[activity_instance]
    ) as worker:
        result = await client.execute_workflow(
            ActivityCallableClassWorkflow.run,
            MyDataClass(field1=", workflow param"),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result == MyDataClass(field1="in worker, workflow param")


async def test_workflow_activity_callable_class_bad_register(client: Client):
    # Try to register the class instead of the instance
    with pytest.raises(TypeError) as err:
        new_worker(
            client, ActivityCallableClassWorkflow, activities=[CallableClassActivity]
        )
    assert "is a class instead of an instance" in str(err.value)


class MethodActivity:
    def __init__(self, orig_field1: str) -> None:
        self.orig_field1 = orig_field1

    @activity.defn(name="custom-name")
    async def add(self, to_add: MyDataClass) -> MyDataClass:
        return MyDataClass(field1=self.orig_field1 + to_add.field1)

    @activity.defn
    async def add_multi(self, source: MyDataClass, to_add: str) -> MyDataClass:
        return MyDataClass(field1=source.field1 + to_add)


@workflow.defn
class ActivityMethodWorkflow:
    @workflow.run
    async def run(self, to_add: MyDataClass) -> MyDataClass:
        ret = await workflow.execute_activity_method(
            MethodActivity.add, to_add, start_to_close_timeout=timedelta(seconds=30)
        )
        return await workflow.execute_activity_method(
            MethodActivity.add_multi,
            args=[ret, ", in workflow"],
            start_to_close_timeout=timedelta(seconds=30),
        )


async def test_workflow_activity_method(client: Client):
    activity_instance = MethodActivity("in worker")
    async with new_worker(
        client,
        ActivityMethodWorkflow,
        activities=[activity_instance.add, activity_instance.add_multi],
    ) as worker:
        result = await client.execute_workflow(
            ActivityMethodWorkflow.run,
            MyDataClass(field1=", workflow param"),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result == MyDataClass(field1="in worker, workflow param, in workflow")


@workflow.defn
class WaitConditionTimeoutWorkflow:
    def __init__(self) -> None:
        self._done = False
        self._waiting = False

    @workflow.run
    async def run(self) -> None:
        # Force timeout, ignore, wait again
        try:
            await workflow.wait_condition(
                lambda: self._done, timeout=0.01, timeout_summary="hi!"
            )
            raise RuntimeError("Expected timeout")
        except asyncio.TimeoutError:
            pass
        self._waiting = True
        await workflow.wait_condition(lambda: self._done)

    @workflow.signal
    def done(self) -> None:
        self._done = True

    @workflow.query
    def waiting(self) -> bool:
        return self._waiting


async def test_workflow_wait_condition_timeout(client: Client):
    async with new_worker(
        client,
        WaitConditionTimeoutWorkflow,
    ) as worker:
        handle = await client.start_workflow(
            WaitConditionTimeoutWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Wait until it's waiting, then send the signal
        async def waiting() -> bool:
            return await handle.query(WaitConditionTimeoutWorkflow.waiting)

        await assert_eq_eventually(True, waiting)
        await handle.signal(WaitConditionTimeoutWorkflow.done)
        # Wait for result which should succeed
        await handle.result()


@workflow.defn
class HelloWorkflowWithQuery:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"

    @workflow.query
    def some_query(self) -> str:
        return "some value"


async def test_workflow_query_rpc_timeout(client: Client):
    # Run workflow under worker and confirm query works
    async with new_worker(
        client,
        HelloWorkflowWithQuery,
    ) as worker:
        handle = await client.start_workflow(
            HelloWorkflowWithQuery.run,
            "Temporal",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert "Hello, Temporal!" == await handle.result()
        assert "some value" == await handle.query(HelloWorkflowWithQuery.some_query)

    # Now with the worker stopped, issue a query with a one second timeout
    with pytest.raises(RPCError) as err:
        await handle.query(
            HelloWorkflowWithQuery.some_query, rpc_timeout=timedelta(seconds=1)
        )
    assert (
        err.value.status == RPCStatusCode.CANCELLED
        and "timeout" in str(err.value).lower()
    ) or err.value.status == RPCStatusCode.DEADLINE_EXCEEDED


@dataclass
class TypedHandleResponse:
    field1: str


@workflow.defn
class TypedHandleWorkflow:
    @workflow.run
    async def run(self) -> TypedHandleResponse:
        return TypedHandleResponse(field1="foo")


async def test_workflow_typed_handle(client: Client):
    async with new_worker(client, TypedHandleWorkflow) as worker:
        # Run the workflow then get a typed handle for it and confirm response
        # type is as expected
        id = f"workflow-{uuid.uuid4()}"
        await client.execute_workflow(
            TypedHandleWorkflow.run, id=id, task_queue=worker.task_queue
        )
        handle_result: TypedHandleResponse = await client.get_workflow_handle_for(
            TypedHandleWorkflow.run,  # type: ignore[arg-type]
            id,
        ).result()
        assert isinstance(handle_result, TypedHandleResponse)


@dataclass
class MemoValue:
    field1: str


@workflow.defn
class MemoWorkflow:
    @workflow.run
    async def run(self, run_child: bool) -> None:
        # Check untyped memo
        assert workflow.memo()["my_memo"] == {"field1": "foo"}
        # Check typed memo
        assert workflow.memo_value("my_memo", type_hint=MemoValue) == MemoValue(
            field1="foo"
        )
        # Check default
        assert workflow.memo_value("absent_memo", "blah") == "blah"
        # Check key error
        try:
            workflow.memo_value("absent_memo")
            assert False
        except KeyError:
            pass
        # Run child if requested
        if run_child:
            await workflow.execute_child_workflow(
                MemoWorkflow.run, False, memo=workflow.memo()
            )


async def test_workflow_memo(client: Client):
    async with new_worker(client, MemoWorkflow) as worker:
        # Run workflow
        handle = await client.start_workflow(
            MemoWorkflow.run,
            True,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            memo={"my_memo": MemoValue(field1="foo")},
        )
        await handle.result()
        desc = await handle.describe()
        # Check untyped memo
        assert (await desc.memo())["my_memo"] == {"field1": "foo"}
        # Check typed memo
        assert (await desc.memo_value("my_memo", type_hint=MemoValue)) == MemoValue(
            field1="foo"
        )
        # Check default
        assert (await desc.memo_value("absent_memo", "blah")) == "blah"
        # Check key error
        try:
            await desc.memo_value("absent_memo")
            assert False
        except KeyError:
            pass


@workflow.defn
class QueryAffectConditionWorkflow:
    def __init__(self) -> None:
        self.seen_query = False

    @workflow.run
    async def run(self) -> None:
        def condition_never_after_query():
            assert not self.seen_query
            return False

        while True:
            await workflow.wait_condition(condition_never_after_query)

    @workflow.query
    def check_condition(self) -> bool:
        # This is a bad thing, to mutate a workflow during a query, this is just
        # for this test
        self.seen_query = True
        return True


async def test_workflow_query_does_not_run_condition(client: Client):
    async with new_worker(client, QueryAffectConditionWorkflow) as worker:
        handle = await client.start_workflow(
            QueryAffectConditionWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.query(QueryAffectConditionWorkflow.check_condition)


@workflow.defn
class CancelSignalAndTimerFiredInSameTaskWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Start a 1 hour timer
        self.timer_task = asyncio.create_task(asyncio.sleep(60 * 60))
        # Wait on it
        try:
            await self.timer_task
            assert False
        except asyncio.CancelledError:
            pass

    @workflow.signal
    def cancel_timer(self) -> None:
        self.timer_task.cancel()


async def test_workflow_cancel_signal_and_timer_fired_in_same_task(
    client: Client, env: WorkflowEnvironment
):
    # This test only works when we support time skipping
    if not env.supports_time_skipping:
        pytest.skip("Need to skip time to validate this test")

    # TODO(cretz): There is a bug in the Java test server, probably
    # https://github.com/temporalio/sdk-java/issues/1138 where the first
    # unlock-and-sleep hangs when running this test after
    # test_workflow_cancel_activity. So we create a new test environment here.
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Start worker for 30 mins. Need to disable workflow cache since we
        # restart the worker and don't want to pay the sticky queue penalty.
        async with new_worker(
            client, CancelSignalAndTimerFiredInSameTaskWorkflow, max_cached_workflows=0
        ) as worker:
            task_queue = worker.task_queue
            handle = await client.start_workflow(
                CancelSignalAndTimerFiredInSameTaskWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            # Wait 30 mins so the worker is waiting on timer
            await env.sleep(30 * 60)

        # Listen to handler result in background so the auto-skipping works
        result_task = asyncio.create_task(handle.result())

        # Now that worker is stopped, send a signal and wait another hour to pass
        # the timer
        await handle.signal(CancelSignalAndTimerFiredInSameTaskWorkflow.cancel_timer)
        await env.sleep(60 * 60)

        # Start worker again and wait for workflow completion
        async with new_worker(
            client,
            CancelSignalAndTimerFiredInSameTaskWorkflow,
            task_queue=task_queue,
            max_cached_workflows=0,
        ):
            # This used to not complete because a signal cancelling the timer was
            # not respected by the timer fire
            await result_task


class MyCustomError(ApplicationError):
    def __init__(self, message: str) -> None:
        super().__init__(message, type="MyCustomError", non_retryable=True)


@activity.defn
async def custom_error_activity() -> NoReturn:
    raise MyCustomError("activity error!")


@workflow.defn
class CustomErrorWorkflow:
    @workflow.run
    async def run(self) -> NoReturn:
        try:
            await workflow.execute_activity(
                custom_error_activity, schedule_to_close_timeout=timedelta(seconds=30)
            )
        except ActivityError:
            raise MyCustomError("workflow error!")


class CustomFailureConverter(DefaultFailureConverterWithEncodedAttributes):
    # We'll override from failure to convert back to our type
    def from_failure(
        self, failure: Failure, payload_converter: PayloadConverter
    ) -> BaseException:
        err = super().from_failure(failure, payload_converter)
        if isinstance(err, ApplicationError) and err.type == "MyCustomError":
            my_err = MyCustomError(err.message)
            my_err.__cause__ = err.__cause__
            err = my_err
        return err


async def test_workflow_custom_failure_converter(client: Client):
    # Clone the client but change the data converter to use our failure
    # converter
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        config["data_converter"],
        failure_converter_class=CustomFailureConverter,
    )
    client = Client(**config)

    # Run workflow and confirm error
    async with new_worker(
        client, CustomErrorWorkflow, activities=[custom_error_activity]
    ) as worker:
        handle = await client.start_workflow(
            CustomErrorWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()

    # Check error is as expected
    assert isinstance(err.value.cause, MyCustomError)
    assert err.value.cause.message == "workflow error!"
    assert isinstance(err.value.cause.cause, ActivityError)
    assert isinstance(err.value.cause.cause.cause, MyCustomError)
    assert err.value.cause.cause.cause.message == "activity error!"
    assert err.value.cause.cause.cause.cause is None

    # Check in history it is encoded
    failure = (
        (await handle.fetch_history())
        .events[-1]
        .workflow_execution_failed_event_attributes.failure
    )
    assert failure.application_failure_info.type == "MyCustomError"
    while True:
        assert failure.message == "Encoded failure"
        assert failure.stack_trace == ""
        attrs: Dict[str, Any] = PayloadConverter.default.from_payloads(
            [failure.encoded_attributes]
        )[0]
        assert "message" in attrs
        assert "stack_trace" in attrs
        if not failure.HasField("cause"):
            break
        failure = failure.cause


@dataclass
class OptionalParam:
    some_string: str


@workflow.defn
class OptionalParamWorkflow:
    @workflow.run
    async def run(
        self, some_param: Optional[OptionalParam] = OptionalParam(some_string="default")
    ) -> Optional[OptionalParam]:
        assert some_param is None or (
            isinstance(some_param, OptionalParam)
            and some_param.some_string in ["default", "foo"]
        )
        return some_param


async def test_workflow_optional_param(client: Client):
    async with new_worker(client, OptionalParamWorkflow) as worker:
        # Don't send a parameter and confirm it is defaulted
        result1 = await client.execute_workflow(
            "OptionalParamWorkflow",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            result_type=OptionalParam,
        )
        assert result1 == OptionalParam(some_string="default")
        # Send None explicitly
        result2 = await client.execute_workflow(
            OptionalParamWorkflow.run,
            None,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result2 is None
        # Send param explicitly
        result3 = await client.execute_workflow(
            OptionalParamWorkflow.run,
            OptionalParam(some_string="foo"),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result3 == OptionalParam(some_string="foo")


class ExceptionRaisingPayloadConverter(DefaultPayloadConverter):
    bad_outbound_str = "bad-outbound-payload-str"
    bad_inbound_str = "bad-inbound-payload-str"

    def to_payloads(self, values: Sequence[Any]) -> List[Payload]:
        if any(
            value == ExceptionRaisingPayloadConverter.bad_outbound_str
            for value in values
        ):
            raise ApplicationError("Intentional outbound converter failure")
        return super().to_payloads(values)

    def from_payloads(
        self, payloads: Sequence[Payload], type_hints: Optional[List] = None
    ) -> List[Any]:
        # Check if any payloads contain the bad data
        for payload in payloads:
            if (
                ExceptionRaisingPayloadConverter.bad_inbound_str.encode()
                in payload.data
            ):
                raise ApplicationError("Intentional inbound converter failure")
        return super().from_payloads(payloads, type_hints)


@workflow.defn
class ExceptionRaisingConverterWorkflow:
    @workflow.run
    async def run(self, some_param: str) -> str:
        return some_param


async def test_exception_raising_converter_param(client: Client):
    # Clone the client but change the data converter to use our converter
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        config["data_converter"],
        payload_converter_class=ExceptionRaisingPayloadConverter,
    )
    client = Client(**config)

    # Run workflow and confirm error
    async with new_worker(client, ExceptionRaisingConverterWorkflow) as worker:
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                ExceptionRaisingConverterWorkflow.run,
                ExceptionRaisingPayloadConverter.bad_inbound_str,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert isinstance(err.value.cause, ApplicationError)
        assert "Intentional inbound converter failure" in str(err.value.cause)


@workflow.defn
class ActivityOutboundConversionFailureWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            "some-activity",
            ExceptionRaisingPayloadConverter.bad_outbound_str,
            start_to_close_timeout=timedelta(seconds=10),
        )


async def test_workflow_activity_outbound_conversion_failure(client: Client):
    # This test used to fail because we created commands _before_ we attempted
    # to convert the arguments thereby causing half-built commands to get sent
    # to the server.

    # Clone the client but change the data converter to use our converter
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        config["data_converter"],
        payload_converter_class=ExceptionRaisingPayloadConverter,
    )
    client = Client(**config)
    async with new_worker(client, ActivityOutboundConversionFailureWorkflow) as worker:
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                ActivityOutboundConversionFailureWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert isinstance(err.value.cause, ApplicationError)
        assert "Intentional outbound converter failure" in str(err.value.cause)


@dataclass
class ManualResultType:
    some_string: str


@activity.defn
async def manual_result_type_activity() -> ManualResultType:
    return ManualResultType(some_string="from-activity")


@workflow.defn
class ManualResultTypeWorkflow:
    @workflow.run
    async def run(self) -> ManualResultType:
        # Only check activity and child if not a child ourselves
        if not workflow.info().parent:
            # Activity without result type and with
            res1 = await workflow.execute_activity(
                "manual_result_type_activity",
                schedule_to_close_timeout=timedelta(minutes=2),
            )
            assert res1 == {"some_string": "from-activity"}
            res2 = await workflow.execute_activity(
                "manual_result_type_activity",
                result_type=ManualResultType,
                schedule_to_close_timeout=timedelta(minutes=2),
            )
            assert res2 == ManualResultType(some_string="from-activity")
            # Child without result type and with
            res3 = await workflow.execute_child_workflow(
                "ManualResultTypeWorkflow",
            )
            assert res3 == {"some_string": "from-workflow"}
            res4 = await workflow.execute_child_workflow(
                "ManualResultTypeWorkflow",
                result_type=ManualResultType,
            )
            assert res4 == ManualResultType(some_string="from-workflow")
        return ManualResultType(some_string="from-workflow")

    @workflow.query
    def some_query(self) -> ManualResultType:
        return ManualResultType(some_string="from-query")


async def test_manual_result_type(client: Client):
    async with new_worker(
        client, ManualResultTypeWorkflow, activities=[manual_result_type_activity]
    ) as worker:
        # Workflow without result type and with
        res1 = await client.execute_workflow(
            "ManualResultTypeWorkflow",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert res1 == {"some_string": "from-workflow"}
        handle = await client.start_workflow(
            "ManualResultTypeWorkflow",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            result_type=ManualResultType,
        )
        res2 = await handle.result()
        assert res2 == ManualResultType(some_string="from-workflow")
        # Query without result type and with
        res3 = await handle.query("some_query")
        assert res3 == {"some_string": "from-query"}
        res4 = await handle.query("some_query", result_type=ManualResultType)
        assert res4 == ManualResultType(some_string="from-query")


@activity.defn
async def wait_forever_activity() -> None:
    await asyncio.Future()


@workflow.defn
class WaitForeverWorkflow:
    @workflow.run
    async def run(self) -> None:
        await asyncio.Future()


@workflow.defn
class CacheEvictionTearDownWorkflow:
    def __init__(self) -> None:
        self._signal_count = 0

    @workflow.run
    async def run(self) -> None:
        # Start several things in background. This is just to show that eviction
        # can work even with these things running.
        tasks = [
            asyncio.create_task(
                workflow.execute_activity(
                    wait_forever_activity, start_to_close_timeout=timedelta(hours=1)
                )
            ),
            asyncio.create_task(
                workflow.execute_child_workflow(WaitForeverWorkflow.run)
            ),
            asyncio.create_task(asyncio.sleep(1000)),
            asyncio.shield(
                workflow.execute_activity(
                    wait_forever_activity, start_to_close_timeout=timedelta(hours=1)
                )
            ),
            asyncio.create_task(workflow.wait_condition(lambda: False)),
        ]
        gather_fut = asyncio.gather(*tasks, return_exceptions=True)
        # Let's also start something in the background that we never wait on
        asyncio.create_task(asyncio.sleep(1000))
        try:
            # Wait for signal count to reach 2
            await asyncio.sleep(0.01)
            await workflow.wait_condition(lambda: self._signal_count > 1)
        finally:
            # This finally, on eviction, is actually called but the command
            # should be ignored
            await asyncio.sleep(0.01)
            await workflow.wait_condition(lambda: self._signal_count > 2)
            # Cancel gather tasks and wait on them, but ignore the errors
            for task in tasks:
                task.cancel()
            await gather_fut

    @workflow.signal
    async def signal(self) -> None:
        self._signal_count += 1

    @workflow.query
    def signal_count(self) -> int:
        return self._signal_count


async def test_cache_eviction_tear_down(client: Client):
    # This test simulates forcing eviction. This used to raise GeneratorExit on
    # GC which triggered the finally which could run on any thread Python
    # chooses, but now we expect eviction to properly tear down tasks and
    # therefore we cancel them
    async with new_worker(
        client,
        CacheEvictionTearDownWorkflow,
        WaitForeverWorkflow,
        activities=[wait_forever_activity],
        max_cached_workflows=0,
    ) as worker:
        # Put a hook to catch unraisable exceptions
        old_hook = sys.unraisablehook
        hook_calls: List[Any] = []
        sys.unraisablehook = hook_calls.append
        try:
            handle = await client.start_workflow(
                CacheEvictionTearDownWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

            async def signal_count() -> int:
                return await handle.query(CacheEvictionTearDownWorkflow.signal_count)

            # Confirm signal count as 0
            await assert_eq_eventually(0, signal_count)

            # Send signal and confirm it's at 1
            await handle.signal(CacheEvictionTearDownWorkflow.signal)
            await assert_eq_eventually(1, signal_count)

            await handle.signal(CacheEvictionTearDownWorkflow.signal)
            await assert_eq_eventually(2, signal_count)

            await handle.signal(CacheEvictionTearDownWorkflow.signal)
            await assert_eq_eventually(3, signal_count)

            await handle.result()
        finally:
            sys.unraisablehook = old_hook

        # Confirm no unraisable exceptions
        assert not hook_calls


@dataclass
class CapturedEvictionException:
    is_replaying: bool
    exception: BaseException


captured_eviction_exceptions: List[CapturedEvictionException] = []


@workflow.defn(sandboxed=False)
class EvictionCaptureExceptionWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Going to sleep so we can force eviction
        try:
            await asyncio.sleep(0.01)
        except BaseException as err:
            captured_eviction_exceptions.append(
                CapturedEvictionException(
                    is_replaying=workflow.unsafe.is_replaying(), exception=err
                )
            )


async def test_workflow_eviction_exception(client: Client):
    assert not captured_eviction_exceptions

    # Run workflow with no cache (forces eviction every step)
    async with new_worker(
        client, EvictionCaptureExceptionWorkflow, max_cached_workflows=0
    ) as worker:
        await client.execute_workflow(
            EvictionCaptureExceptionWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

    # Confirm expected eviction replaying state and exception type
    assert len(captured_eviction_exceptions) == 1
    assert captured_eviction_exceptions[0].is_replaying
    assert (
        type(captured_eviction_exceptions[0].exception).__name__
        == "_WorkflowBeingEvictedError"
    )


@dataclass
class DynamicWorkflowValue:
    some_string: str


@workflow.defn(dynamic=True)
class DynamicWorkflow:
    @workflow.run
    async def run(self, args: Sequence[RawValue]) -> DynamicWorkflowValue:
        assert len(args) == 2
        arg1 = workflow.payload_converter().from_payload(
            args[0].payload, DynamicWorkflowValue
        )
        assert isinstance(arg1, DynamicWorkflowValue)
        arg2 = workflow.payload_converter().from_payload(
            args[1].payload, DynamicWorkflowValue
        )
        assert isinstance(arg1, DynamicWorkflowValue)
        return DynamicWorkflowValue(
            f"{workflow.info().workflow_type} - {arg1.some_string} - {arg2.some_string}"
        )


async def test_workflow_dynamic(client: Client):
    async with new_worker(client, DynamicWorkflow) as worker:
        result = await client.execute_workflow(
            "some-workflow",
            args=[DynamicWorkflowValue("val1"), DynamicWorkflowValue("val2")],
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            result_type=DynamicWorkflowValue,
        )
        assert isinstance(result, DynamicWorkflowValue)
        assert result == DynamicWorkflowValue("some-workflow - val1 - val2")


@workflow.defn
class QueriesDoingBadThingsWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    @workflow.query
    async def bad_query(self, bad_thing: str) -> str:
        if bad_thing == "wait_condition":
            await workflow.wait_condition(lambda: True)
        elif bad_thing == "continue_as_new":
            workflow.continue_as_new()
        elif bad_thing == "upsert_search_attribute":
            workflow.upsert_search_attributes({"foo": ["bar"]})
        elif bad_thing == "start_activity":
            workflow.start_activity(
                "some-activity", start_to_close_timeout=timedelta(minutes=10)
            )
        elif bad_thing == "start_child_workflow":
            await workflow.start_child_workflow("some-workflow")
        elif bad_thing == "random":
            workflow.random().random()
        elif bad_thing == "set_query_handler":
            workflow.set_query_handler("some-handler", lambda: "whatever")
        elif bad_thing == "patch":
            workflow.patched("some-patch")
        elif bad_thing == "signal_external_handle":
            await workflow.get_external_workflow_handle("some-id").signal("some-signal")
        return "should never get here"


async def test_workflow_queries_doing_bad_things(client: Client):
    async with new_worker(client, QueriesDoingBadThingsWorkflow) as worker:
        handle = await client.start_workflow(
            QueriesDoingBadThingsWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        async def assert_bad_query(bad_thing: str) -> None:
            with pytest.raises(WorkflowQueryFailedError) as err:
                _ = await handle.query(
                    QueriesDoingBadThingsWorkflow.bad_query, bad_thing
                )
            assert "While in read-only function, action attempted" in str(err)

        await assert_bad_query("wait_condition")
        await assert_bad_query("continue_as_new")
        await assert_bad_query("upsert_search_attribute")
        await assert_bad_query("start_activity")
        await assert_bad_query("start_child_workflow")
        await assert_bad_query("random")
        await assert_bad_query("set_query_handler")
        await assert_bad_query("patch")
        await assert_bad_query("signal_external_handle")


# typing.Self only in 3.11+
if sys.version_info >= (3, 11):

    @dataclass
    class AnnotatedWithSelfParam:
        some_str: str

    @workflow.defn
    class WorkflowAnnotatedWithSelf:
        @workflow.run
        async def run(self: typing.Self, some_arg: AnnotatedWithSelfParam) -> str:
            assert isinstance(some_arg, AnnotatedWithSelfParam)
            return some_arg.some_str

    async def test_workflow_annotated_with_self(client: Client):
        async with new_worker(client, WorkflowAnnotatedWithSelf) as worker:
            assert "foo" == await client.execute_workflow(
                WorkflowAnnotatedWithSelf.run,
                AnnotatedWithSelfParam(some_str="foo"),
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )


@activity.defn
async def custom_metrics_activity() -> None:
    counter = activity.metric_meter().create_counter(
        "my-activity-counter", "my-activity-description", "my-activity-unit"
    )
    counter.add(12)
    counter.add(34, {"my-activity-extra-attr": 12.34})


@workflow.defn
class CustomMetricsWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            custom_metrics_activity, schedule_to_close_timeout=timedelta(seconds=30)
        )

        histogram = workflow.metric_meter().create_histogram(
            "my-workflow-histogram", "my-workflow-description", "my-workflow-unit"
        )
        histogram.record(56)
        histogram.with_additional_attributes({"my-workflow-extra-attr": 1234}).record(
            78
        )


async def test_workflow_custom_metrics(client: Client):
    # Run worker with default runtime which is noop meter just to confirm it
    # doesn't fail
    async with new_worker(
        client, CustomMetricsWorkflow, activities=[custom_metrics_activity]
    ) as worker:
        await client.execute_workflow(
            CustomMetricsWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

    # Create new runtime with Prom server
    prom_addr = f"127.0.0.1:{find_free_port()}"
    runtime = Runtime(
        telemetry=TelemetryConfig(
            metrics=PrometheusConfig(bind_address=prom_addr), metric_prefix="foo_"
        )
    )

    # Confirm meter fails with bad attribute type
    with pytest.raises(TypeError) as err:
        runtime.metric_meter.with_additional_attributes({"some_attr": None})  # type: ignore
    assert str(err.value).startswith("Invalid value type for key")

    # New client with the runtime
    client = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=runtime,
    )

    async with new_worker(
        client, CustomMetricsWorkflow, activities=[custom_metrics_activity]
    ) as worker:
        # Record a gauge at runtime level
        gauge = runtime.metric_meter.with_additional_attributes(
            {"my-runtime-extra-attr1": "val1", "my-runtime-extra-attr2": True}
        ).create_gauge("my-runtime-gauge", "my-runtime-description")
        gauge.set(90)

        # Run workflow
        await client.execute_workflow(
            CustomMetricsWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Get Prom dump
        with urlopen(url=f"http://{prom_addr}/metrics") as f:
            prom_str: str = f.read().decode("utf-8")
            prom_lines = prom_str.splitlines()

        # Intentionally naive metric checker
        def matches_metric_line(
            line: str, name: str, at_least_labels: Mapping[str, str], value: int
        ) -> bool:
            # Must have metric name
            if not line.startswith(name + "{"):
                return False
            # Must have labels (don't escape for this test)
            for k, v in at_least_labels.items():
                if f'{k}="{v}"' not in line:
                    return False
            return line.endswith(f" {value}")

        def assert_metric_exists(
            name: str, at_least_labels: Mapping[str, str], value: int
        ) -> None:
            assert any(
                matches_metric_line(line, name, at_least_labels, value)
                for line in prom_lines
            )

        def assert_description_exists(name: str, description: str) -> None:
            assert f"# HELP {name} {description}" in prom_lines

        # Check some metrics are as we expect
        assert_description_exists("my_runtime_gauge", "my-runtime-description")
        assert_metric_exists(
            "my_runtime_gauge",
            {
                "my_runtime_extra_attr1": "val1",
                "my_runtime_extra_attr2": "true",
                # Also confirm global service name label
                "service_name": "temporal-core-sdk",
            },
            90,
        )
        assert_description_exists("my_workflow_histogram", "my-workflow-description")
        assert_metric_exists("my_workflow_histogram_sum", {}, 56)
        assert_metric_exists(
            "my_workflow_histogram_sum",
            {
                "my_workflow_extra_attr": "1234",
                # Also confirm some workflow labels
                "namespace": client.namespace,
                "task_queue": worker.task_queue,
                "workflow_type": "CustomMetricsWorkflow",
            },
            78,
        )
        assert_description_exists("my_activity_counter", "my-activity-description")
        assert_metric_exists("my_activity_counter", {}, 12)
        assert_metric_exists(
            "my_activity_counter",
            {
                "my_activity_extra_attr": "12.34",
                # Also confirm some activity labels
                "namespace": client.namespace,
                "task_queue": worker.task_queue,
                "activity_type": "custom_metrics_activity",
            },
            34,
        )
        # Also check Temporal metric got its prefix
        assert_metric_exists(
            "foo_workflow_completed", {"workflow_type": "CustomMetricsWorkflow"}, 1
        )


async def test_workflow_buffered_metrics(client: Client):
    # Create runtime with metric buffer
    buffer = MetricBuffer(10000)
    runtime = Runtime(
        telemetry=TelemetryConfig(metrics=buffer, metric_prefix="some_prefix_")
    )

    # Confirm no updates yet
    assert not buffer.retrieve_updates()

    # Create a counter and make one with more attrs
    runtime_counter = runtime.metric_meter.create_counter(
        "runtime-counter", "runtime-counter-desc", "runtime-counter-unit"
    )
    runtime_counter_with_attrs = runtime_counter.with_additional_attributes(
        {"foo": "bar", "baz": 123}
    )

    # Send adds to both
    runtime_counter.add(100)
    runtime_counter_with_attrs.add(200)

    # Get updates and check their values
    runtime_updates1 = buffer.retrieve_updates()
    assert len(runtime_updates1) == 2
    # Check that the metric fields are right
    assert runtime_updates1[0].metric.name == "runtime-counter"
    assert runtime_updates1[0].metric.description == "runtime-counter-desc"
    assert runtime_updates1[0].metric.unit == "runtime-counter-unit"
    assert runtime_updates1[0].metric.kind == BUFFERED_METRIC_KIND_COUNTER
    # Check that the metric is the exact same object all the way from Rust
    assert id(runtime_updates1[0].metric) == id(runtime_updates1[1].metric)
    # Check the values and attributes
    assert runtime_updates1[0].value == 100
    assert runtime_updates1[0].attributes == {"service_name": "temporal-core-sdk"}
    assert runtime_updates1[1].value == 200
    assert runtime_updates1[1].attributes == {
        "service_name": "temporal-core-sdk",
        "foo": "bar",
        "baz": 123,
    }

    # Confirm no more updates
    assert not buffer.retrieve_updates()

    # Send some more adds and check
    runtime_counter.add(300)
    runtime_counter_with_attrs.add(400)
    runtime_updates2 = buffer.retrieve_updates()
    assert len(runtime_updates2)
    # Check that metrics are the same exact object as before
    assert id(runtime_updates1[0].metric) == id(runtime_updates2[0].metric)
    assert id(runtime_updates1[1].metric) == id(runtime_updates2[1].metric)
    # Check that even the attribute dictionaries are exact same objects as before
    assert id(runtime_updates1[0].attributes) == id(runtime_updates2[0].attributes)
    assert id(runtime_updates1[1].attributes) == id(runtime_updates2[1].attributes)
    # Check values
    assert runtime_updates2[0].value == 300
    assert runtime_updates2[1].value == 400

    # Create a new client on the runtime and execute the custom metric workflow
    client = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=runtime,
    )
    async with new_worker(
        client, CustomMetricsWorkflow, activities=[custom_metrics_activity]
    ) as worker:
        await client.execute_workflow(
            CustomMetricsWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

    # Drain updates and confirm updates exist as expected
    updates = buffer.retrieve_updates()
    # Workflow update histogram, with some extra sanity checks
    assert any(
        update.metric.name == "my-workflow-histogram"
        and update.metric.description == "my-workflow-description"
        and update.metric.unit == "my-workflow-unit"
        and update.metric.kind == BUFFERED_METRIC_KIND_HISTOGRAM
        and update.attributes["namespace"] == client.namespace
        and update.attributes["task_queue"] == worker.task_queue
        and update.attributes["workflow_type"] == "CustomMetricsWorkflow"
        and "my-workflow-extra-attr" not in update.attributes
        and update.value == 56
        for update in updates
    )
    assert any(
        update.metric.name == "my-workflow-histogram"
        and update.attributes.get("my-workflow-extra-attr") == 1234
        and update.value == 78
        for update in updates
    )
    # Check activity counter too
    assert any(
        update.metric.name == "my-activity-counter"
        and update.metric.description == "my-activity-description"
        and update.metric.unit == "my-activity-unit"
        and update.metric.kind == BUFFERED_METRIC_KIND_COUNTER
        and update.attributes["namespace"] == client.namespace
        and update.attributes["task_queue"] == worker.task_queue
        and update.attributes["activity_type"] == "custom_metrics_activity"
        and "my-activity-extra-attr" not in update.attributes
        and update.value == 12
        for update in updates
    )
    assert any(
        update.metric.name == "my-activity-counter"
        and update.attributes.get("my-activity-extra-attr") == 12.34
        and update.value == 34
        for update in updates
    )
    # Check for a Temporal metric too
    assert any(
        update.metric.name == "some_prefix_workflow_completed"
        and update.attributes["workflow_type"] == "CustomMetricsWorkflow"
        and update.value == 1
        for update in updates
    )


async def test_workflow_metrics_other_types(client: Client):
    async def do_stuff(buffer: MetricBuffer) -> None:
        runtime = Runtime(telemetry=TelemetryConfig(metrics=buffer))
        new_client = await Client.connect(
            client.service_client.config.target_host,
            namespace=client.namespace,
            runtime=runtime,
        )
        async with new_worker(new_client, HelloWorkflow) as worker:
            await new_client.execute_workflow(
                HelloWorkflow.run,
                "Temporal",
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        # Also, add some manual types beyond the defaults tested in other tests
        runtime.metric_meter.create_histogram_float("my-histogram-float").record(1.23)
        runtime.metric_meter.create_histogram_timedelta(
            "my-histogram-timedelta"
        ).record(timedelta(days=2, seconds=3, milliseconds=4))
        runtime.metric_meter.create_gauge_float("my-gauge-float").set(4.56)

    # Create a buffer, do stuff, check the metrics
    buffer = MetricBuffer(10000)
    await do_stuff(buffer)
    updates = buffer.retrieve_updates()
    assert any(
        u.metric.name == "temporal_workflow_task_execution_latency"
        # Took more than 3ms
        and u.value > 3
        and isinstance(u.value, int)
        and u.metric.unit == "ms"
        for u in updates
    )
    assert any(
        u.metric.name == "my-histogram-float"
        and u.value == 1.23
        and isinstance(u.value, float)
        for u in updates
    )
    assert any(
        u.metric.name == "my-histogram-timedelta"
        and u.value
        == int(timedelta(days=2, seconds=3, milliseconds=4).total_seconds() * 1000)
        and isinstance(u.value, int)
        for u in updates
    )
    assert any(
        u.metric.name == "my-gauge-float"
        and u.value == 4.56
        and isinstance(u.value, float)
        for u in updates
    )

    # Do it again with seconds
    buffer = MetricBuffer(10000, duration_format=MetricBufferDurationFormat.SECONDS)
    await do_stuff(buffer)
    updates = buffer.retrieve_updates()
    assert any(
        u.metric.name == "temporal_workflow_task_execution_latency"
        # Took less than 3s
        and u.value < 3
        and isinstance(u.value, float)
        and u.metric.unit == "s"
        for u in updates
    )
    assert any(
        u.metric.name == "my-histogram-timedelta"
        and u.value == timedelta(days=2, seconds=3, milliseconds=4).total_seconds()
        and isinstance(u.value, float)
        for u in updates
    )


bad_validator_fail_ct = 0
task_fail_ct = 0


@workflow.defn
class UpdateHandlersWorkflow:
    def __init__(self) -> None:
        self._last_event: Optional[str] = None

    @workflow.run
    async def run(self) -> None:
        workflow.set_update_handler("first_task_update", lambda: "worked")

        # Wait forever
        await asyncio.Future()

    @workflow.update
    def last_event(self, an_arg: str) -> str:
        if an_arg == "fail":
            raise ApplicationError("SyncFail")
        le = self._last_event or "<no event>"
        self._last_event = an_arg
        return le

    @last_event.validator
    def last_event_validator(self, an_arg: str) -> None:
        workflow.logger.info("Running validator with arg %s", an_arg)
        if an_arg == "reject_me":
            raise ApplicationError("Rejected")

    @workflow.update
    async def last_event_async(self, an_arg: str) -> str:
        await asyncio.sleep(1)
        if an_arg == "fail":
            raise ApplicationError("AsyncFail")
        le = self._last_event or "<no event>"
        self._last_event = an_arg
        return le

    @workflow.update
    async def runs_activity(self, name: str) -> str:
        act = workflow.start_activity(
            say_hello, name, schedule_to_close_timeout=timedelta(seconds=5)
        )
        act.cancel()
        await act
        return "done"

    @workflow.update(name="renamed")
    async def async_named(self) -> str:
        return "named"

    @workflow.update
    async def bad_validator(self) -> str:
        return "done"

    @bad_validator.validator
    def bad_validator_validator(self) -> None:
        global bad_validator_fail_ct
        # Run a command which should not be allowed the first few tries, then "fix" it as if new code was deployed
        if bad_validator_fail_ct < 2:
            bad_validator_fail_ct += 1
            workflow.start_activity(
                say_hello, "boo", schedule_to_close_timeout=timedelta(seconds=5)
            )

    @workflow.update
    async def set_dynamic(self) -> str:
        def dynahandler(name: str, _args: Sequence[RawValue]) -> str:
            return "dynahandler - " + name

        def dynavalidator(name: str, _args: Sequence[RawValue]) -> None:
            if name == "reject_me":
                raise ApplicationError("Rejected")

        workflow.set_dynamic_update_handler(dynahandler, validator=dynavalidator)
        return "set"

    @workflow.update
    def throws_runtime_err(self) -> None:
        global task_fail_ct
        if task_fail_ct < 1:
            task_fail_ct += 1
            raise RuntimeError("intentional failure")


async def test_workflow_update_handlers_happy(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    async with new_worker(
        client, UpdateHandlersWorkflow, activities=[say_hello]
    ) as worker:
        wf_id = f"update-handlers-workflow-{uuid.uuid4()}"
        handle = await client.start_workflow(
            UpdateHandlersWorkflow.run,
            id=wf_id,
            task_queue=worker.task_queue,
        )

        # Dynamically registered and used in first task
        # TODO: Once https://github.com/temporalio/sdk-python/issues/462 is fixed, uncomment
        # assert "worked" == await handle.execute_update("first_task_update")

        # Normal handling
        last_event = await handle.execute_update(
            UpdateHandlersWorkflow.last_event, "val2"
        )
        assert "<no event>" == last_event

        # Async handler
        last_event = await handle.execute_update(
            UpdateHandlersWorkflow.last_event_async, "val3"
        )
        assert "val2" == last_event

        # Dynamic handler
        await handle.execute_update(UpdateHandlersWorkflow.set_dynamic)
        assert "dynahandler - made_up" == await handle.execute_update("made_up")

        # Name overload
        assert "named" == await handle.execute_update(
            UpdateHandlersWorkflow.async_named
        )

        # Get untyped handle
        assert "val3" == await client.get_workflow_handle(wf_id).execute_update(
            UpdateHandlersWorkflow.last_event, "val4"
        )


async def test_workflow_update_handlers_unhappy(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    async with new_worker(client, UpdateHandlersWorkflow) as worker:
        handle = await client.start_workflow(
            UpdateHandlersWorkflow.run,
            id=f"update-handlers-workflow-unhappy-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Undefined handler
        with pytest.raises(WorkflowUpdateFailedError) as err:
            await handle.execute_update("whargarbl", "whatever")
        assert isinstance(err.value.cause, ApplicationError)
        assert "'whargarbl' expected but not found" in err.value.cause.message
        # TODO: Once https://github.com/temporalio/sdk-python/issues/462 is fixed, uncomment
        # assert (
        #     "known updates: [bad_validator first_task_update last_event last_event_async renamed runs_activity set_dynamic throws_runtime_err]"
        #     in err.value.cause.message
        # )

        # Rejection by validator
        with pytest.raises(WorkflowUpdateFailedError) as err:
            await handle.execute_update(UpdateHandlersWorkflow.last_event, "reject_me")
        assert isinstance(err.value.cause, ApplicationError)
        assert "Rejected" == err.value.cause.message

        # Failure during update handler
        with pytest.raises(WorkflowUpdateFailedError) as err:
            await handle.execute_update(UpdateHandlersWorkflow.last_event, "fail")
        assert isinstance(err.value.cause, ApplicationError)
        assert "SyncFail" == err.value.cause.message

        with pytest.raises(WorkflowUpdateFailedError) as err:
            await handle.execute_update(UpdateHandlersWorkflow.last_event_async, "fail")
        assert isinstance(err.value.cause, ApplicationError)
        assert "AsyncFail" == err.value.cause.message

        # Cancel inside handler
        with pytest.raises(WorkflowUpdateFailedError) as err:
            await handle.execute_update(UpdateHandlersWorkflow.runs_activity, "foo")
        assert isinstance(err.value.cause, CancelledError)

        # Incorrect args for handler
        with pytest.raises(WorkflowUpdateFailedError) as err:
            await handle.execute_update("last_event", args=[121, "badarg"])
        assert isinstance(err.value.cause, ApplicationError)
        assert (
            "last_event_validator() takes 2 positional arguments but 3 were given"
            in err.value.cause.message
        )

        # Un-deserializeable nonsense
        with pytest.raises(WorkflowUpdateFailedError) as err:
            await handle.execute_update(
                "last_event",
                arg=RawValue(
                    payload=Payload(
                        metadata={"encoding": b"u-dont-know-me"}, data=b"enchi-cat"
                    )
                ),
            )
        assert isinstance(err.value.cause, ApplicationError)
        assert "Failed decoding arguments" == err.value.cause.message

        # Dynamic handler
        await handle.execute_update(UpdateHandlersWorkflow.set_dynamic)

        # Rejection by dynamic handler validator
        with pytest.raises(WorkflowUpdateFailedError) as err:
            await handle.execute_update("reject_me")
        assert isinstance(err.value.cause, ApplicationError)
        assert "Rejected" == err.value.cause.message


async def test_workflow_update_task_fails(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    # Need to not sandbox so behavior can change based on globals
    async with new_worker(
        client, UpdateHandlersWorkflow, workflow_runner=UnsandboxedWorkflowRunner()
    ) as worker:
        handle = await client.start_workflow(
            UpdateHandlersWorkflow.run,
            id=f"update-handlers-command-in-validator-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            task_timeout=timedelta(seconds=1),
        )

        # This will produce a WFT failure which will eventually resolve and then this
        # update will return
        res = await handle.execute_update(UpdateHandlersWorkflow.bad_validator)
        assert res == "done"

        # Non-temporal failure should cause task failure in update handler
        await handle.execute_update(UpdateHandlersWorkflow.throws_runtime_err)

        # Verify task failures did happen
        global task_fail_ct, bad_validator_fail_ct
        assert task_fail_ct == 1
        assert bad_validator_fail_ct == 2


@workflow.defn
class UpdateRespectsFirstExecutionRunIdWorkflow:
    def __init__(self) -> None:
        self.update_received = False

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self.update_received)

    @workflow.update
    async def update(self) -> None:
        self.update_received = True


async def test_workflow_update_respects_first_execution_run_id(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    # Start one workflow, obtain the run ID (r1), and let it complete. Start a second
    # workflow with the same workflow ID, and try to send an update using the handle from
    # r1.
    workflow_id = f"update-respects-first-execution-run-id-{uuid.uuid4()}"
    async with new_worker(client, UpdateRespectsFirstExecutionRunIdWorkflow) as worker:

        async def start_workflow(workflow_id: str) -> WorkflowHandle:
            return await client.start_workflow(
                UpdateRespectsFirstExecutionRunIdWorkflow.run,
                id=workflow_id,
                task_queue=worker.task_queue,
            )

        wf_execution_1_handle = await start_workflow(workflow_id)
        await wf_execution_1_handle.execute_update(
            UpdateRespectsFirstExecutionRunIdWorkflow.update
        )
        await wf_execution_1_handle.result()
        await start_workflow(workflow_id)

        # Execution 1 has closed. This would succeed if the update incorrectly targets
        # the second execution
        with pytest.raises(RPCError) as exc_info:
            await wf_execution_1_handle.execute_update(
                UpdateRespectsFirstExecutionRunIdWorkflow.update
            )
        assert exc_info.value.status == RPCStatusCode.NOT_FOUND
        assert "workflow execution not found" in str(exc_info.value)


@workflow.defn
class ImmediatelyCompleteUpdateAndWorkflow:
    def __init__(self) -> None:
        self._got_update = "no"

    @workflow.run
    async def run(self) -> str:
        return "workflow-done"

    @workflow.update
    async def update(self) -> str:
        self._got_update = "yes"
        return "update-done"

    @workflow.query
    def got_update(self) -> str:
        return self._got_update


async def test_workflow_update_before_worker_start(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    # In order to confirm that all started workflows get updates before the
    # workflow completes, this test will start a workflow and start an update.
    # Only then will it start the worker to process both in the task. The
    # workflow and update should both succeed properly. This also invokes a
    # query to confirm update mutation. We do this with the cache off to confirm
    # replay behavior.

    # Start workflow
    task_queue = f"tq-{uuid.uuid4()}"
    handle = await client.start_workflow(
        ImmediatelyCompleteUpdateAndWorkflow.run,
        id=f"wf-{uuid.uuid4()}",
        task_queue=task_queue,
    )

    # Confirm update not there
    assert not await workflow_update_exists(client, handle.id, "my-update")

    # Execute update in background
    update_task = asyncio.create_task(
        handle.execute_update(
            ImmediatelyCompleteUpdateAndWorkflow.update, id="my-update"
        )
    )

    # Wait until update exists
    await assert_eq_eventually(
        True, lambda: workflow_update_exists(client, handle.id, "my-update")
    )

    # Start no-cache worker on the task queue
    async with new_worker(
        client,
        ImmediatelyCompleteUpdateAndWorkflow,
        task_queue=task_queue,
        max_cached_workflows=0,
    ):
        # Confirm workflow completed as expected
        assert "workflow-done" == await handle.result()
        assert "update-done" == await update_task
        assert "yes" == await handle.query(
            ImmediatelyCompleteUpdateAndWorkflow.got_update
        )


@workflow.defn
class UpdateSeparateHandleWorkflow:
    def __init__(self) -> None:
        self._complete = False
        self._complete_update = False

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self._complete)
        return "workflow-done"

    @workflow.update
    async def update(self) -> str:
        await workflow.wait_condition(lambda: self._complete_update)
        self._complete = True
        return "update-done"

    @workflow.signal
    async def signal(self) -> None:
        self._complete_update = True


async def test_workflow_update_separate_handle(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    async with new_worker(client, UpdateSeparateHandleWorkflow) as worker:
        # Start the workflow
        handle = await client.start_workflow(
            UpdateSeparateHandleWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Start an update waiting on accepted
        update_handle_1 = await handle.start_update(
            UpdateSeparateHandleWorkflow.update,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
        )

        assert update_handle_1.workflow_run_id == handle.first_execution_run_id

        # Create another handle and have them both wait for update complete
        update_handle_2 = client.get_workflow_handle(
            handle.id, run_id=handle.result_run_id
        ).get_update_handle_for(UpdateSeparateHandleWorkflow.update, update_handle_1.id)
        update_handle_task1 = asyncio.create_task(update_handle_1.result())
        update_handle_task2 = asyncio.create_task(update_handle_2.result())

        # Signal completion and confirm all completed as expected
        await handle.signal(UpdateSeparateHandleWorkflow.signal)
        assert "update-done" == await update_handle_task1
        assert "update-done" == await update_handle_task2
        assert "workflow-done" == await handle.result()


@workflow.defn
class UpdateTimeoutOrCancelWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)

    @workflow.update
    async def do_update(self, sleep: float) -> None:
        await asyncio.sleep(sleep)


async def test_workflow_update_timeout_or_cancel(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )

    # Confirm start timeout via short timeout on update w/ no worker running
    handle = await client.start_workflow(
        UpdateTimeoutOrCancelWorkflow.run,
        id=f"wf-{uuid.uuid4()}",
        task_queue="does-not-exist",
    )
    with pytest.raises(WorkflowUpdateRPCTimeoutOrCancelledError):
        await handle.start_update(
            UpdateTimeoutOrCancelWorkflow.do_update,
            1000,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            rpc_timeout=timedelta(milliseconds=1),
        )

    # Confirm start cancel via cancel on update w/ no worker running
    handle = await client.start_workflow(
        UpdateTimeoutOrCancelWorkflow.run,
        id=f"wf-{uuid.uuid4()}",
        task_queue="does-not-exist",
    )
    task = asyncio.create_task(
        handle.start_update(
            UpdateTimeoutOrCancelWorkflow.do_update,
            1000,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
            id="my-update",
        )
    )
    # Have to wait for update to exist before cancelling to capture
    await assert_eq_eventually(
        True, lambda: workflow_update_exists(client, handle.id, "my-update")
    )
    task.cancel()
    with pytest.raises(WorkflowUpdateRPCTimeoutOrCancelledError):
        await task

    # Start worker
    async with new_worker(client, UpdateTimeoutOrCancelWorkflow) as worker:
        # Start the workflow
        handle = await client.start_workflow(
            UpdateTimeoutOrCancelWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # Start an update
        update_handle = await handle.start_update(
            UpdateTimeoutOrCancelWorkflow.do_update,
            1000,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
        )
        # Timeout a poll call
        with pytest.raises(WorkflowUpdateRPCTimeoutOrCancelledError):
            await update_handle.result(rpc_timeout=timedelta(milliseconds=1))

        # Cancel a poll call
        update_handle = await handle.start_update(
            UpdateTimeoutOrCancelWorkflow.do_update,
            1000,
            wait_for_stage=WorkflowUpdateStage.ACCEPTED,
        )
        result_task = asyncio.create_task(update_handle.result())
        # Unfortunately there is not a way for us to confirm this is actually
        # pending the server call and if you cancel early you get an asyncio
        # cancelled error because it never even reached the gRPC client. We
        # considered sleeping, but that makes for flaky tests. So what we are
        # going to do is patch the poll call to notify us when it was called.
        called = asyncio.Event()
        unpatched_call = client.workflow_service.poll_workflow_execution_update

        async def patched_call(*args, **kwargs):
            called.set()
            return await unpatched_call(*args, **kwargs)

        client.workflow_service.poll_workflow_execution_update = patched_call  # type: ignore
        try:
            await called.wait()
        finally:
            client.workflow_service.poll_workflow_execution_update = unpatched_call
        result_task.cancel()
        with pytest.raises(WorkflowUpdateRPCTimeoutOrCancelledError):
            await result_task


@workflow.defn
class TimeoutSupportWorkflow:
    @workflow.run
    async def run(self, approach: str) -> None:
        if sys.version_info < (3, 11):
            raise RuntimeError("Timeout only in >= 3.11")
        if approach == "timeout":
            async with asyncio.timeout(0.2):
                await workflow.execute_activity(
                    wait_cancel, schedule_to_close_timeout=timedelta(seconds=20)
                )
        elif approach == "timeout_at":
            async with asyncio.timeout_at(asyncio.get_running_loop().time() + 0.2):
                await workflow.execute_activity(
                    wait_cancel, schedule_to_close_timeout=timedelta(seconds=20)
                )
        elif approach == "wait_for":
            await asyncio.wait_for(
                workflow.execute_activity(
                    wait_cancel, schedule_to_close_timeout=timedelta(seconds=20)
                ),
                0.2,
            )
        elif approach == "call_later":
            activity_task = asyncio.create_task(
                workflow.execute_activity(
                    wait_cancel, schedule_to_close_timeout=timedelta(seconds=20)
                )
            )
            asyncio.get_running_loop().call_later(0.2, activity_task.cancel)
            await activity_task
        elif approach == "call_at":
            activity_task = asyncio.create_task(
                workflow.execute_activity(
                    wait_cancel, schedule_to_close_timeout=timedelta(seconds=20)
                )
            )
            asyncio.get_running_loop().call_at(
                asyncio.get_running_loop().time() + 0.2, activity_task.cancel
            )
            await activity_task
        else:
            raise RuntimeError(f"Unrecognized approach: {approach}")


@pytest.mark.parametrize(
    "approach", ["timeout", "timeout_at", "wait_for", "call_later", "call_at"]
)
async def test_workflow_timeout_support(client: Client, approach: str):
    if sys.version_info < (3, 11):
        pytest.skip("Timeout only in >= 3.11")
    async with new_worker(
        client, TimeoutSupportWorkflow, activities=[wait_cancel]
    ) as worker:
        # Run and confirm activity gets cancelled
        handle = await client.start_workflow(
            TimeoutSupportWorkflow.run,
            approach,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()
        assert isinstance(err.value.cause, ActivityError)
        assert isinstance(err.value.cause.cause, CancelledError)
        # Check that no matter which approach, it made a 300ms timer
        found_timer = False
        async for e in handle.fetch_history_events():
            if e.HasField("timer_started_event_attributes"):
                assert (
                    e.timer_started_event_attributes.start_to_fire_timeout.ToMilliseconds()
                    == 200
                )
                found_timer = True
                break
        assert found_timer


@workflow.defn
class BuildIDInfoWorkflow:
    do_finish = False

    @workflow.run
    async def run(self):
        await asyncio.sleep(1)
        if workflow.info().get_current_build_id() == "1.0":
            await workflow.execute_activity(
                say_hello, "yo", schedule_to_close_timeout=timedelta(seconds=5)
            )
        await workflow.wait_condition(lambda: self.do_finish)

    @workflow.query
    def get_build_id(self) -> str:
        return workflow.info().get_current_build_id()

    @workflow.signal
    async def finish(self):
        self.do_finish = True


async def test_workflow_current_build_id_appropriately_set(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("Java test server does not support worker versioning")

    task_queue = str(uuid.uuid4())
    async with new_worker(
        client,
        BuildIDInfoWorkflow,
        activities=[say_hello],
        build_id="1.0",
        task_queue=task_queue,
    ) as worker:
        handle = await client.start_workflow(
            BuildIDInfoWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        bid = await handle.query(BuildIDInfoWorkflow.get_build_id)
        assert bid == "1.0"
        await worker.shutdown()

    await client.workflow_service.reset_sticky_task_queue(
        ResetStickyTaskQueueRequest(
            namespace=client.namespace,
            execution=WorkflowExecution(workflow_id=handle.id),
        )
    )

    async with new_worker(
        client,
        BuildIDInfoWorkflow,
        activities=[say_hello],
        build_id="1.1",
        task_queue=task_queue,
    ) as worker:
        bid = await handle.query(BuildIDInfoWorkflow.get_build_id)
        assert bid == "1.0"
        await handle.signal(BuildIDInfoWorkflow.finish)
        bid = await handle.query(BuildIDInfoWorkflow.get_build_id)
        assert bid == "1.1"
        await handle.result()
        bid = await handle.query(BuildIDInfoWorkflow.get_build_id)
        assert bid == "1.1"

        await worker.shutdown()


class FailureTypesScenario(IntEnum):
    THROW_CUSTOM_EXCEPTION = 1
    CAUSE_NON_DETERMINISM = 2
    WAIT_FOREVER = 3


class FailureTypesCustomException(Exception): ...


class FailureTypesWorkflowBase(ABC):
    async def run(self, scenario: FailureTypesScenario) -> None:
        await self._apply_scenario(scenario)

    @workflow.signal
    async def signal(self, scenario: FailureTypesScenario) -> None:
        await self._apply_scenario(scenario)

    @workflow.update
    async def update(self, scenario: FailureTypesScenario) -> None:
        # We have to rollover the task so the task failure isn't treated as
        # non-acceptance
        await asyncio.sleep(0.01)
        await self._apply_scenario(scenario)

    async def _apply_scenario(self, scenario: FailureTypesScenario) -> None:
        if scenario == FailureTypesScenario.THROW_CUSTOM_EXCEPTION:
            raise FailureTypesCustomException("Intentional exception")
        elif scenario == FailureTypesScenario.CAUSE_NON_DETERMINISM:
            if not workflow.unsafe.is_replaying():
                await asyncio.sleep(0.01)
        elif scenario == FailureTypesScenario.WAIT_FOREVER:
            await workflow.wait_condition(lambda: False)


@workflow.defn
class FailureTypesUnconfiguredWorkflow(FailureTypesWorkflowBase):
    @workflow.run
    async def run(self, scenario: FailureTypesScenario) -> None:
        await super().run(scenario)


@workflow.defn(
    failure_exception_types=[FailureTypesCustomException, workflow.NondeterminismError]
)
class FailureTypesConfiguredExplicitlyWorkflow(FailureTypesWorkflowBase):
    @workflow.run
    async def run(self, scenario: FailureTypesScenario) -> None:
        await super().run(scenario)


@workflow.defn(failure_exception_types=[Exception])
class FailureTypesConfiguredInheritedWorkflow(FailureTypesWorkflowBase):
    @workflow.run
    async def run(self, scenario: FailureTypesScenario) -> None:
        await super().run(scenario)


async def test_workflow_failure_types_configured(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )

    # Asserter for a single scenario
    async def assert_scenario(
        workflow: Type[FailureTypesWorkflowBase],
        *,
        expect_task_fail: bool,
        fail_message_contains: str,
        worker_level_failure_exception_type: Optional[Type[Exception]] = None,
        workflow_scenario: Optional[FailureTypesScenario] = None,
        signal_scenario: Optional[FailureTypesScenario] = None,
        update_scenario: Optional[FailureTypesScenario] = None,
    ) -> None:
        logging.debug(
            "Asserting scenario %s",
            {
                "workflow": workflow,
                "expect_task_fail": expect_task_fail,
                "fail_message_contains": fail_message_contains,
                "worker_level_failure_exception_type": worker_level_failure_exception_type,
                "workflow_scenario": workflow_scenario,
                "signal_scenario": signal_scenario,
                "update_scenario": update_scenario,
            },
        )
        async with new_worker(
            client,
            workflow,
            max_cached_workflows=0,
            workflow_failure_exception_types=[worker_level_failure_exception_type]
            if worker_level_failure_exception_type
            else [],
        ) as worker:
            # Start workflow
            handle = await client.start_workflow(
                workflow.run,
                workflow_scenario or FailureTypesScenario.WAIT_FOREVER,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            if signal_scenario:
                await handle.signal(workflow.signal, signal_scenario)
            update_handle: Optional[WorkflowUpdateHandle[Any]] = None
            if update_scenario:
                update_handle = await handle.start_update(
                    workflow.update,
                    update_scenario,
                    wait_for_stage=WorkflowUpdateStage.ACCEPTED,
                    id="my-update-1",
                )

            # Expect task or exception fail
            if expect_task_fail:

                async def has_expected_task_fail() -> bool:
                    async for e in handle.fetch_history_events():
                        if (
                            e.HasField("workflow_task_failed_event_attributes")
                            and fail_message_contains
                            in e.workflow_task_failed_event_attributes.failure.message
                        ):
                            return True
                    return False

                await assert_eq_eventually(
                    True, has_expected_task_fail, timeout=timedelta(seconds=45)
                )
            else:
                with pytest.raises(TemporalError) as err:
                    # Update does not throw on non-determinism, the workflow
                    # does instead
                    if (
                        update_handle
                        and update_scenario
                        == FailureTypesScenario.THROW_CUSTOM_EXCEPTION
                    ):
                        await update_handle.result()
                    else:
                        await handle.result()
                assert isinstance(err.value.cause, ApplicationError)
                assert fail_message_contains in err.value.cause.message

    # Run a scenario
    async def run_scenario(
        workflow: Type[FailureTypesWorkflowBase],
        scenario: FailureTypesScenario,
        *,
        expect_task_fail: bool = False,
        worker_level_failure_exception_type: Optional[Type[Exception]] = None,
    ) -> None:
        # Run for workflow, signal, and update
        fail_message_contains = (
            "Intentional exception"
            if scenario == FailureTypesScenario.THROW_CUSTOM_EXCEPTION
            else "Nondeterminism"
        )
        await assert_scenario(
            workflow,
            expect_task_fail=expect_task_fail,
            fail_message_contains=fail_message_contains,
            worker_level_failure_exception_type=worker_level_failure_exception_type,
            workflow_scenario=scenario,
        )
        await assert_scenario(
            workflow,
            expect_task_fail=expect_task_fail,
            fail_message_contains=fail_message_contains,
            worker_level_failure_exception_type=worker_level_failure_exception_type,
            signal_scenario=scenario,
        )
        await assert_scenario(
            workflow,
            expect_task_fail=expect_task_fail,
            fail_message_contains=fail_message_contains,
            worker_level_failure_exception_type=worker_level_failure_exception_type,
            update_scenario=scenario,
        )

        # When unconfigured completely, confirm task fails as normal
        await run_scenario(
            FailureTypesUnconfiguredWorkflow,
            FailureTypesScenario.THROW_CUSTOM_EXCEPTION,
            expect_task_fail=True,
        )
        await run_scenario(
            FailureTypesUnconfiguredWorkflow,
            FailureTypesScenario.CAUSE_NON_DETERMINISM,
            expect_task_fail=True,
        )
        # When configured at the worker level explicitly, confirm not task fail
        # but rather expected exceptions
        await run_scenario(
            FailureTypesUnconfiguredWorkflow,
            FailureTypesScenario.THROW_CUSTOM_EXCEPTION,
            worker_level_failure_exception_type=FailureTypesCustomException,
        )
        await run_scenario(
            FailureTypesUnconfiguredWorkflow,
            FailureTypesScenario.CAUSE_NON_DETERMINISM,
            worker_level_failure_exception_type=temporalio.workflow.NondeterminismError,
        )
        # When configured at the worker level inherited
        await run_scenario(
            FailureTypesUnconfiguredWorkflow,
            FailureTypesScenario.THROW_CUSTOM_EXCEPTION,
            worker_level_failure_exception_type=Exception,
        )
        await run_scenario(
            FailureTypesUnconfiguredWorkflow,
            FailureTypesScenario.CAUSE_NON_DETERMINISM,
            worker_level_failure_exception_type=Exception,
        )
        # When configured at the workflow level explicitly
        await run_scenario(
            FailureTypesConfiguredExplicitlyWorkflow,
            FailureTypesScenario.THROW_CUSTOM_EXCEPTION,
        )
        await run_scenario(
            FailureTypesConfiguredExplicitlyWorkflow,
            FailureTypesScenario.CAUSE_NON_DETERMINISM,
        )
        # When configured at the workflow level inherited
        await run_scenario(
            FailureTypesConfiguredInheritedWorkflow,
            FailureTypesScenario.THROW_CUSTOM_EXCEPTION,
        )
        await run_scenario(
            FailureTypesConfiguredInheritedWorkflow,
            FailureTypesScenario.CAUSE_NON_DETERMINISM,
        )


@workflow.defn(failure_exception_types=[Exception])
class FailOnBadInputWorkflow:
    @workflow.run
    async def run(self, param: str) -> None:
        pass


async def test_workflow_fail_on_bad_input(client: Client):
    async with new_worker(client, FailOnBadInputWorkflow) as worker:
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                "FailOnBadInputWorkflow",
                123,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
    assert isinstance(err.value.cause, ApplicationError)
    assert "Failed decoding arguments" in err.value.cause.message


@workflow.defn
class TickingWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Just tick every 100ms for 10s
        for _ in range(100):
            await asyncio.sleep(0.1)


async def test_workflow_replace_worker_client(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Only testing against two real servers")
    # We are going to start a second ephemeral server and then replace the
    # client. So we will start a no-cache ticking workflow with the current
    # client and confirm it has accomplished at least one task. Then we will
    # start another on the other client, and confirm it gets started too. Then
    # we will terminate both. We have to use a ticking workflow with only one
    # poller to force a quick re-poll to recognize our client change quickly (as
    # opposed to just waiting the minute for poll timeout).
    async with await WorkflowEnvironment.start_local() as other_env:
        # Start both workflows on different servers
        task_queue = f"tq-{uuid.uuid4()}"
        handle1 = await client.start_workflow(
            TickingWorkflow.run, id=f"wf-{uuid.uuid4()}", task_queue=task_queue
        )
        handle2 = await other_env.client.start_workflow(
            TickingWorkflow.run, id=f"wf-{uuid.uuid4()}", task_queue=task_queue
        )

        async def any_task_completed(handle: WorkflowHandle) -> bool:
            async for e in handle.fetch_history_events():
                if e.HasField("workflow_task_completed_event_attributes"):
                    return True
            return False

        # Now start the worker on the first env
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[TickingWorkflow],
            max_cached_workflows=0,
            max_concurrent_workflow_task_polls=1,
        ) as worker:
            # Confirm the first ticking workflow has completed a task but not
            # the second
            await assert_eq_eventually(True, lambda: any_task_completed(handle1))
            assert not await any_task_completed(handle2)

            # Now replace the client, which should be used fairly quickly
            # because we should have timer-done poll completions every 100ms
            worker.client = other_env.client

            # Now confirm the other workflow has started
            await assert_eq_eventually(True, lambda: any_task_completed(handle2))

            # Terminate both
            await handle1.terminate()
            await handle2.terminate()


@activity.defn(dynamic=True)
async def return_name_activity(args: Sequence[RawValue]) -> str:
    return activity.info().activity_type


@workflow.defn
class AsCompletedWorkflow:
    @workflow.run
    async def run(self) -> List[str]:
        # Lazily start 10 different activities and wait for each completed
        tasks = [
            workflow.execute_activity(
                f"my-activity-{i}", start_to_close_timeout=timedelta(seconds=1)
            )
            for i in range(10)
        ]

        # Using asyncio.as_completed like below almost always fails with
        # non-determinism error because it uses sets internally, but we can't
        # assert on that because it could _technically_ pass though unlikely:
        # return [await task for task in asyncio.as_completed(tasks)]

        return [await task for task in workflow.as_completed(tasks)]


async def test_workflow_as_completed_utility(client: Client):
    # Disable cache to force replay
    async with new_worker(
        client,
        AsCompletedWorkflow,
        activities=[return_name_activity],
        max_cached_workflows=0,
    ) as worker:
        # This would fail if we used asyncio.as_completed in the workflow
        result = await client.execute_workflow(
            AsCompletedWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert len(result) == 10


@workflow.defn
class WaitWorkflow:
    @workflow.run
    async def run(self) -> List[str]:
        # Create 10 tasks that return activity names, wait on them, then execute
        # the activities
        async def new_activity_name(index: int) -> str:
            return f"my-activity-{index}"

        name_tasks = [asyncio.create_task(new_activity_name(i)) for i in range(10)]

        # Using asyncio.wait like below almost always fails with non-determinism
        # error because it returns sets, but we can't assert on that because it
        # could _technically_ pass though unlikely:
        # done, _ = await asyncio.wait(name_tasks)

        done, _ = await workflow.wait(name_tasks)
        return [
            await workflow.execute_activity(
                await activity_name, start_to_close_timeout=timedelta(seconds=1)
            )
            for activity_name in done
        ]


async def test_workflow_wait_utility(client: Client):
    # Disable cache to force replay
    async with new_worker(
        client, WaitWorkflow, activities=[return_name_activity], max_cached_workflows=0
    ) as worker:
        # This would fail if we used asyncio.wait in the workflow
        result = await client.execute_workflow(
            WaitWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert len(result) == 10


@workflow.defn
class CurrentUpdateWorkflow:
    def __init__(self) -> None:
        self._pending_get_update_id_tasks: List[asyncio.Task[str]] = []

    @workflow.run
    async def run(self) -> List[str]:
        # Confirm no update info
        assert not workflow.current_update_info()

        # Wait for all tasks to come in, then return the full set
        await workflow.wait_condition(
            lambda: len(self._pending_get_update_id_tasks) == 5
        )
        assert not workflow.current_update_info()
        return list(await asyncio.gather(*self._pending_get_update_id_tasks))

    @workflow.update
    async def do_update(self) -> str:
        # Check that simple helper awaited has the ID
        info = workflow.current_update_info()
        assert info
        assert info.name == "do_update"
        assert info.id == await self.get_update_id()

        # Also schedule the task and wait for it in the main workflow to confirm
        # it still gets the update ID
        self._pending_get_update_id_tasks.append(
            asyncio.create_task(self.get_update_id())
        )

        # Re-fetch and return
        info = workflow.current_update_info()
        assert info
        return info.id

    @do_update.validator
    def do_update_validator(self) -> None:
        info = workflow.current_update_info()
        assert info
        assert info.name == "do_update"

    async def get_update_id(self) -> str:
        await asyncio.sleep(0.01)
        info = workflow.current_update_info()
        assert info
        return info.id


async def test_workflow_current_update(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    async with new_worker(client, CurrentUpdateWorkflow) as worker:
        handle = await client.start_workflow(
            CurrentUpdateWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        update_ids = await asyncio.gather(
            handle.execute_update(CurrentUpdateWorkflow.do_update, id="update1"),
            handle.execute_update(CurrentUpdateWorkflow.do_update, id="update2"),
            handle.execute_update(CurrentUpdateWorkflow.do_update, id="update3"),
            handle.execute_update(CurrentUpdateWorkflow.do_update, id="update4"),
            handle.execute_update(CurrentUpdateWorkflow.do_update, id="update5"),
        )
        assert {"update1", "update2", "update3", "update4", "update5"} == set(
            update_ids
        )
        assert {"update1", "update2", "update3", "update4", "update5"} == set(
            await handle.result()
        )


def skip_unfinished_handler_tests_in_older_python():
    # These tests reliably fail or timeout in 3.9
    if sys.version_info < (3, 10):
        pytest.skip("Skipping unfinished handler tests in Python < 3.10")


@workflow.defn
class UnfinishedHandlersWarningsWorkflow:
    def __init__(self):
        self.started_handler = False
        self.handler_may_return = False
        self.handler_finished = False

    @workflow.run
    async def run(self, wait_all_handlers_finished: bool) -> bool:
        await workflow.wait_condition(lambda: self.started_handler)
        if wait_all_handlers_finished:
            self.handler_may_return = True
            await workflow.wait_condition(workflow.all_handlers_finished)
        return self.handler_finished

    async def _do_update_or_signal(self) -> None:
        self.started_handler = True
        await workflow.wait_condition(lambda: self.handler_may_return)
        self.handler_finished = True

    @workflow.update
    async def my_update(self) -> None:
        await self._do_update_or_signal()

    @workflow.update(unfinished_policy=workflow.HandlerUnfinishedPolicy.ABANDON)
    async def my_update_ABANDON(self) -> None:
        await self._do_update_or_signal()

    @workflow.update(
        unfinished_policy=workflow.HandlerUnfinishedPolicy.WARN_AND_ABANDON
    )
    async def my_update_WARN_AND_ABANDON(self) -> None:
        await self._do_update_or_signal()

    @workflow.signal
    async def my_signal(self):
        await self._do_update_or_signal()

    @workflow.signal(unfinished_policy=workflow.HandlerUnfinishedPolicy.ABANDON)
    async def my_signal_ABANDON(self):
        await self._do_update_or_signal()

    @workflow.signal(
        unfinished_policy=workflow.HandlerUnfinishedPolicy.WARN_AND_ABANDON
    )
    async def my_signal_WARN_AND_ABANDON(self):
        await self._do_update_or_signal()


async def test_unfinished_update_handler(client: Client, env: WorkflowEnvironment):
    skip_unfinished_handler_tests_in_older_python()
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    async with new_worker(client, UnfinishedHandlersWarningsWorkflow) as worker:
        test = _UnfinishedHandlersWarningsTest(client, worker, "update")
        await test.test_wait_all_handlers_finished_and_unfinished_handlers_warning()
        await test.test_unfinished_handlers_cause_exceptions_in_test_suite()


async def test_unfinished_signal_handler(client: Client):
    skip_unfinished_handler_tests_in_older_python()
    async with new_worker(client, UnfinishedHandlersWarningsWorkflow) as worker:
        test = _UnfinishedHandlersWarningsTest(client, worker, "signal")
        await test.test_wait_all_handlers_finished_and_unfinished_handlers_warning()
        await test.test_unfinished_handlers_cause_exceptions_in_test_suite()


@dataclass
class _UnfinishedHandlersWarningsTest:
    client: Client
    worker: Worker
    handler_type: Literal["update", "signal"]

    async def test_wait_all_handlers_finished_and_unfinished_handlers_warning(self):
        # The unfinished handler warning is issued by default,
        handler_finished, warning = await self._get_workflow_result_and_warning(
            wait_all_handlers_finished=False,
        )
        assert not handler_finished and warning
        # and when the workflow sets the unfinished_policy to WARN_AND_ABANDON,
        handler_finished, warning = await self._get_workflow_result_and_warning(
            wait_all_handlers_finished=False,
            unfinished_policy=workflow.HandlerUnfinishedPolicy.WARN_AND_ABANDON,
        )
        assert not handler_finished and warning
        # but not when the workflow waits for handlers to complete,
        handler_finished, warning = await self._get_workflow_result_and_warning(
            wait_all_handlers_finished=True,
        )
        assert handler_finished and not warning
        # nor when the silence-warnings policy is set on the handler.
        handler_finished, warning = await self._get_workflow_result_and_warning(
            wait_all_handlers_finished=False,
            unfinished_policy=workflow.HandlerUnfinishedPolicy.ABANDON,
        )
        assert not handler_finished and not warning

    async def test_unfinished_handlers_cause_exceptions_in_test_suite(self):
        # If we don't capture warnings then -- since the unfinished handler warning is converted to
        # an exception in the test suite -- we see WFT failures when we don't wait for handlers.
        handle: asyncio.Future[WorkflowHandle] = asyncio.Future()
        asyncio.create_task(
            self._get_workflow_result(
                wait_all_handlers_finished=False, handle_future=handle
            )
        )
        await assert_eq_eventually(
            True,
            partial(self._workflow_task_failed, workflow_id=(await handle).id),
            timeout=timedelta(seconds=20),
        )

    async def _workflow_task_failed(self, workflow_id: str) -> bool:
        resp = await self.client.workflow_service.get_workflow_execution_history(
            GetWorkflowExecutionHistoryRequest(
                namespace=self.client.namespace,
                execution=WorkflowExecution(workflow_id=workflow_id),
            ),
        )
        for event in reversed(resp.history.events):
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED:
                assert event.workflow_task_failed_event_attributes.failure.message.startswith(
                    f"[TMPRL1102] Workflow finished while {self.handler_type} handlers are still running"
                )
                return True
        return False

    async def _get_workflow_result_and_warning(
        self,
        wait_all_handlers_finished: bool,
        unfinished_policy: Optional[workflow.HandlerUnfinishedPolicy] = None,
    ) -> Tuple[bool, bool]:
        with pytest.WarningsRecorder() as warnings:
            wf_result = await self._get_workflow_result(
                wait_all_handlers_finished, unfinished_policy
            )
            unfinished_handler_warning_emitted = any(
                issubclass(w.category, self._unfinished_handler_warning_cls)
                for w in warnings
            )
            return wf_result, unfinished_handler_warning_emitted

    async def _get_workflow_result(
        self,
        wait_all_handlers_finished: bool,
        unfinished_policy: Optional[workflow.HandlerUnfinishedPolicy] = None,
        handle_future: Optional[asyncio.Future[WorkflowHandle]] = None,
    ) -> bool:
        handle = await self.client.start_workflow(
            UnfinishedHandlersWarningsWorkflow.run,
            arg=wait_all_handlers_finished,
            id=f"wf-{uuid.uuid4()}",
            task_queue=self.worker.task_queue,
        )
        if handle_future:
            handle_future.set_result(handle)
        handler_name = f"my_{self.handler_type}"
        if unfinished_policy:
            handler_name += f"_{unfinished_policy.name}"
        if self.handler_type == "signal":
            await handle.signal(handler_name)
        else:
            if not wait_all_handlers_finished:
                with pytest.raises(WorkflowUpdateFailedError) as err_info:
                    await handle.execute_update(handler_name, id="my-update")
                update_err = err_info.value
                assert isinstance(update_err.cause, ApplicationError)
                assert update_err.cause.type == "AcceptedUpdateCompletedWorkflow"
            else:
                await handle.execute_update(handler_name, id="my-update")

        return await handle.result()

    @property
    def _unfinished_handler_warning_cls(self) -> Type:
        return {
            "update": workflow.UnfinishedUpdateHandlersWarning,
            "signal": workflow.UnfinishedSignalHandlersWarning,
        }[self.handler_type]


@workflow.defn
class UnfinishedHandlersOnWorkflowTerminationWorkflow:
    def __init__(self) -> None:
        self.handlers_may_finish = False

    @workflow.run
    async def run(
        self,
        workflow_termination_type: Literal[
            "-cancellation-",
            "-failure-",
            "-continue-as-new-",
            "-fail-post-continue-as-new-run-",
        ],
        handler_registration: Literal["-late-registered-", "-not-late-registered-"],
        handler_dynamism: Literal["-dynamic-", "-not-dynamic-"],
        handler_waiting: Literal[
            "-wait-all-handlers-finish-", "-no-wait-all-handlers-finish-"
        ],
    ) -> NoReturn:
        if handler_registration == "-late-registered-":
            if handler_dynamism == "-dynamic-":

                async def my_late_registered_dynamic_update(
                    name: str, args: Sequence[RawValue]
                ) -> str:
                    await workflow.wait_condition(lambda: self.handlers_may_finish)
                    return "my-late-registered-dynamic-update-result"

                async def my_late_registered_dynamic_signal(
                    name: str, args: Sequence[RawValue]
                ) -> None:
                    await workflow.wait_condition(lambda: self.handlers_may_finish)

                workflow.set_dynamic_update_handler(my_late_registered_dynamic_update)
                workflow.set_dynamic_signal_handler(my_late_registered_dynamic_signal)
            else:

                async def my_late_registered_update() -> str:
                    await workflow.wait_condition(lambda: self.handlers_may_finish)
                    return "my-late-registered-update-result"

                async def my_late_registered_signal() -> None:
                    await workflow.wait_condition(lambda: self.handlers_may_finish)

                workflow.set_update_handler(
                    "my_late_registered_update", my_late_registered_update
                )
                workflow.set_signal_handler(
                    "my_late_registered_signal", my_late_registered_signal
                )

        if handler_waiting == "-wait-all-handlers-finish-":
            self.handlers_may_finish = True
            await workflow.wait_condition(workflow.all_handlers_finished)
        if workflow_termination_type == "-failure-":
            raise ApplicationError(
                "Deliberately failing workflow with an unfinished handler"
            )
        elif workflow_termination_type == "-fail-post-continue-as-new-run-":
            raise ApplicationError("Deliberately failing post-ContinueAsNew run")
        elif workflow_termination_type == "-continue-as-new-":
            # Fail next run so that test terminates
            workflow.continue_as_new(
                args=[
                    "-fail-post-continue-as-new-run-",
                    handler_registration,
                    handler_dynamism,
                    handler_waiting,
                ]
            )
        else:
            await workflow.wait_condition(lambda: False)
            raise AssertionError("unreachable")

    @workflow.update
    async def my_update(self) -> str:
        await workflow.wait_condition(lambda: self.handlers_may_finish)
        return "update-result"

    @workflow.signal
    async def my_signal(self) -> None:
        await workflow.wait_condition(lambda: self.handlers_may_finish)

    @workflow.update(dynamic=True)
    async def my_dynamic_update(self, name: str, args: Sequence[RawValue]) -> str:
        await workflow.wait_condition(lambda: self.handlers_may_finish)
        return "my-dynamic-update-result"

    @workflow.signal(dynamic=True)
    async def my_dynamic_signal(self, name: str, args: Sequence[RawValue]) -> None:
        await workflow.wait_condition(lambda: self.handlers_may_finish)


@pytest.mark.parametrize("handler_type", ["-signal-", "-update-"])
@pytest.mark.parametrize(
    "handler_registration", ["-late-registered-", "-not-late-registered-"]
)
@pytest.mark.parametrize("handler_dynamism", ["-dynamic-", "-not-dynamic-"])
@pytest.mark.parametrize(
    "handler_waiting",
    ["-wait-all-handlers-finish-", "-no-wait-all-handlers-finish-"],
)
@pytest.mark.parametrize(
    "workflow_termination_type", ["-cancellation-", "-failure-", "-continue-as-new-"]
)
async def test_unfinished_handler_on_workflow_termination(
    client: Client,
    env: WorkflowEnvironment,
    handler_type: Literal["-signal-", "-update-"],
    handler_registration: Literal["-late-registered-", "-not-late-registered-"],
    handler_dynamism: Literal["-dynamic-", "-not-dynamic-"],
    handler_waiting: Literal[
        "-wait-all-handlers-finish-", "-no-wait-all-handlers-finish-"
    ],
    workflow_termination_type: Literal[
        "-cancellation-", "-failure-", "-continue-as-new-"
    ],
):
    skip_unfinished_handler_tests_in_older_python()
    if handler_type == "-update-" and env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    await _UnfinishedHandlersOnWorkflowTerminationTest(
        client,
        handler_type,
        workflow_termination_type,
        handler_registration,
        handler_dynamism,
        handler_waiting,
    ).test_warning_is_issued_on_exit_with_unfinished_handler()


@dataclass
class _UnfinishedHandlersOnWorkflowTerminationTest:
    client: Client
    handler_type: Literal["-signal-", "-update-"]
    workflow_termination_type: Literal[
        "-cancellation-", "-failure-", "-continue-as-new-"
    ]
    handler_registration: Literal["-late-registered-", "-not-late-registered-"]
    handler_dynamism: Literal["-dynamic-", "-not-dynamic-"]
    handler_waiting: Literal[
        "-wait-all-handlers-finish-", "-no-wait-all-handlers-finish-"
    ]

    async def test_warning_is_issued_on_exit_with_unfinished_handler(
        self,
    ):
        assert await self._run_workflow_and_get_warning() == (
            self.handler_waiting == "-no-wait-all-handlers-finish-"
        )

    async def _run_workflow_and_get_warning(self) -> bool:
        workflow_id = f"wf-{uuid.uuid4()}"
        update_id = "update-id"
        task_queue = "tq"

        # We require a startWorkflow, an update, and maybe a cancellation request, to be delivered
        # in the same WFT. To do this we start the worker after they've all been accepted by the
        # server.
        handle = await self.client.start_workflow(
            UnfinishedHandlersOnWorkflowTerminationWorkflow.run,
            args=[
                self.workflow_termination_type,
                self.handler_registration,
                self.handler_dynamism,
                self.handler_waiting,
            ],
            id=workflow_id,
            task_queue=task_queue,
        )
        if self.workflow_termination_type == "-cancellation-":
            await handle.cancel()

        if self.handler_type == "-update-":
            update_method = (
                "__does_not_exist__"
                if self.handler_dynamism == "-dynamic-"
                else "my_late_registered_update"
                if self.handler_registration == "-late-registered-"
                else UnfinishedHandlersOnWorkflowTerminationWorkflow.my_update
            )
            update_task = asyncio.create_task(
                handle.execute_update(
                    update_method,  # type: ignore
                    id=update_id,
                )
            )
            await assert_eq_eventually(
                True,
                lambda: workflow_update_exists(self.client, workflow_id, update_id),
            )
        else:
            signal_method = (
                "__does_not_exist__"
                if self.handler_dynamism == "-dynamic-"
                else "my_late_registered_signal"
                if self.handler_registration == "-late-registered-"
                else UnfinishedHandlersOnWorkflowTerminationWorkflow.my_signal
            )
            await handle.signal(signal_method)  # type: ignore

        async with new_worker(
            self.client,
            UnfinishedHandlersOnWorkflowTerminationWorkflow,
            task_queue=task_queue,
        ):
            with pytest.WarningsRecorder() as warnings:
                if self.handler_type == "-update-":
                    assert update_task
                    if self.handler_waiting == "-wait-all-handlers-finish-":
                        await update_task
                    else:
                        with pytest.raises(WorkflowUpdateFailedError) as err_info:
                            await update_task
                        update_err = err_info.value
                        assert isinstance(update_err.cause, ApplicationError)
                        assert (
                            update_err.cause.type == "AcceptedUpdateCompletedWorkflow"
                        )

                with pytest.raises(WorkflowFailureError) as err:
                    await handle.result()
                assert isinstance(
                    err.value.cause,
                    {
                        "-cancellation-": CancelledError,
                        "-continue-as-new-": ApplicationError,
                        "-failure-": ApplicationError,
                    }[self.workflow_termination_type],
                )
                if self.workflow_termination_type == "-continue-as-new-":
                    assert (
                        str(err.value.cause)
                        == "Deliberately failing post-ContinueAsNew run"
                    )

                unfinished_handler_warning_emitted = any(
                    issubclass(w.category, self._unfinished_handler_warning_cls)
                    for w in warnings
                )
                return unfinished_handler_warning_emitted

    @property
    def _unfinished_handler_warning_cls(self) -> Type:
        return {
            "-update-": workflow.UnfinishedUpdateHandlersWarning,
            "-signal-": workflow.UnfinishedSignalHandlersWarning,
        }[self.handler_type]


@workflow.defn
class IDConflictWorkflow:
    # Just run forever
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


async def test_workflow_id_conflict(client: Client):
    async with new_worker(client, IDConflictWorkflow) as worker:
        # Start a workflow
        handle = await client.start_workflow(
            IDConflictWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        handle = client.get_workflow_handle_for(
            IDConflictWorkflow.run, handle.id, run_id=handle.result_run_id
        )

        # Confirm another fails by default
        with pytest.raises(WorkflowAlreadyStartedError):
            await client.start_workflow(
                IDConflictWorkflow.run,
                id=handle.id,
                task_queue=worker.task_queue,
            )

        # Confirm fails if explicitly given that option
        with pytest.raises(WorkflowAlreadyStartedError):
            await client.start_workflow(
                IDConflictWorkflow.run,
                id=handle.id,
                task_queue=worker.task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )

        # Confirm gives back same handle if requested
        new_handle = await client.start_workflow(
            IDConflictWorkflow.run,
            id=handle.id,
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
        new_handle = client.get_workflow_handle_for(
            IDConflictWorkflow.run, new_handle.id, run_id=new_handle.result_run_id
        )
        assert new_handle.run_id == handle.run_id
        assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
        assert (await new_handle.describe()).status == WorkflowExecutionStatus.RUNNING

        # Confirm terminates and starts new if requested
        new_handle = await client.start_workflow(
            IDConflictWorkflow.run,
            id=handle.id,
            task_queue=worker.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.TERMINATE_EXISTING,
        )
        new_handle = client.get_workflow_handle_for(
            IDConflictWorkflow.run, new_handle.id, run_id=new_handle.result_run_id
        )
        assert new_handle.run_id != handle.run_id
        assert (await handle.describe()).status == WorkflowExecutionStatus.TERMINATED
        assert (await new_handle.describe()).status == WorkflowExecutionStatus.RUNNING


@workflow.defn
class UpdateCompletionIsHonoredWhenAfterWorkflowReturn1Workflow:
    def __init__(self) -> None:
        self.workflow_returned = False

    @workflow.run
    async def run(self) -> str:
        self.workflow_returned = True
        return "workflow-result"

    @workflow.update
    async def my_update(self) -> str:
        await workflow.wait_condition(lambda: self.workflow_returned)
        return "update-result"


async def test_update_completion_is_honored_when_after_workflow_return_1(
    client: Client,
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    update_id = "my-update"
    task_queue = "tq"
    wf_handle = await client.start_workflow(
        UpdateCompletionIsHonoredWhenAfterWorkflowReturn1Workflow.run,
        id=f"wf-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    update_result_task = asyncio.create_task(
        wf_handle.execute_update(
            UpdateCompletionIsHonoredWhenAfterWorkflowReturn1Workflow.my_update,
            id=update_id,
        )
    )
    await workflow_update_exists(client, wf_handle.id, update_id)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[UpdateCompletionIsHonoredWhenAfterWorkflowReturn1Workflow],
    ):
        assert await wf_handle.result() == "workflow-result"
        assert await update_result_task == "update-result"


@workflow.defn
class UpdateCompletionIsHonoredWhenAfterWorkflowReturnWorkflow2:
    def __init__(self):
        self.received_update = False
        self.update_result: asyncio.Future[str] = asyncio.Future()

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self.received_update)
        self.update_result.set_result("update-result")
        # Prior to https://github.com/temporalio/features/issues/481, the client
        # waiting on the update got a "Workflow execution already completed"
        # error instead of the update result, because the main workflow
        # coroutine completion command is emitted before the update completion
        # command, and we were truncating commands at the first completion
        # command.
        return "workflow-result"

    @workflow.update
    async def my_update(self) -> str:
        self.received_update = True
        return await self.update_result


async def test_update_completion_is_honored_when_after_workflow_return_2(
    client: Client,
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )
    async with Worker(
        client,
        task_queue="tq",
        workflows=[UpdateCompletionIsHonoredWhenAfterWorkflowReturnWorkflow2],
    ) as worker:
        handle = await client.start_workflow(
            UpdateCompletionIsHonoredWhenAfterWorkflowReturnWorkflow2.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        update_result = await handle.execute_update(
            UpdateCompletionIsHonoredWhenAfterWorkflowReturnWorkflow2.my_update
        )
        assert update_result == "update-result"
        assert await handle.result() == "workflow-result"


@workflow.defn
class FirstCompletionCommandIsHonoredWorkflow:
    def __init__(self, main_workflow_returns_before_signal_completions=False) -> None:
        self.seen_first_signal = False
        self.seen_second_signal = False
        self.main_workflow_returns_before_signal_completions = (
            main_workflow_returns_before_signal_completions
        )
        self.ping_pong_val = 1
        self.ping_pong_counter = 0
        self.ping_pong_max_count = 4

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(
            lambda: self.seen_first_signal and self.seen_second_signal
        )
        return "workflow-result"

    @workflow.signal
    async def this_signal_executes_first(self):
        self.seen_first_signal = True
        if self.main_workflow_returns_before_signal_completions:
            await self.ping_pong(lambda: self.ping_pong_val > 0)
        raise ApplicationError(
            "Client should see this error unless doing ping-pong "
            "(in which case main coroutine returns first)"
        )

    @workflow.signal
    async def this_signal_executes_second(self):
        await workflow.wait_condition(lambda: self.seen_first_signal)
        self.seen_second_signal = True
        if self.main_workflow_returns_before_signal_completions:
            await self.ping_pong(lambda: self.ping_pong_val < 0)
        raise ApplicationError("Client should never see this error!")

    async def ping_pong(self, cond: Callable[[], bool]):
        while self.ping_pong_counter < self.ping_pong_max_count:
            await workflow.wait_condition(cond)
            self.ping_pong_val = -self.ping_pong_val
            self.ping_pong_counter += 1


@workflow.defn
class FirstCompletionCommandIsHonoredPingPongWorkflow(
    FirstCompletionCommandIsHonoredWorkflow
):
    def __init__(self) -> None:
        super().__init__(main_workflow_returns_before_signal_completions=True)

    @workflow.run
    async def run(self) -> str:
        return await super().run()


async def test_first_of_two_signal_completion_commands_is_honored(client: Client):
    await _do_first_completion_command_is_honored_test(
        client, main_workflow_returns_before_signal_completions=False
    )


async def test_workflow_return_is_honored_when_it_precedes_signal_completion_command(
    client: Client,
):
    await _do_first_completion_command_is_honored_test(
        client, main_workflow_returns_before_signal_completions=True
    )


async def _do_first_completion_command_is_honored_test(
    client: Client, main_workflow_returns_before_signal_completions: bool
):
    workflow_cls: Union[
        Type[FirstCompletionCommandIsHonoredPingPongWorkflow],
        Type[FirstCompletionCommandIsHonoredWorkflow],
    ] = (
        FirstCompletionCommandIsHonoredPingPongWorkflow
        if main_workflow_returns_before_signal_completions
        else FirstCompletionCommandIsHonoredWorkflow
    )
    async with Worker(
        client,
        task_queue="tq",
        workflows=[workflow_cls],
    ) as worker:
        handle = await client.start_workflow(
            workflow_cls.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(workflow_cls.this_signal_executes_second)
        await handle.signal(workflow_cls.this_signal_executes_first)
        try:
            result = await handle.result()
        except WorkflowFailureError as err:
            if main_workflow_returns_before_signal_completions:
                assert (
                    False
                ), "Expected no error due to main workflow coroutine returning first"
            else:
                assert str(err.cause).startswith("Client should see this error")
        else:
            assert (
                main_workflow_returns_before_signal_completions
                and result == "workflow-result"
            )


@workflow.defn
class TimerStartedAfterWorkflowCompletionWorkflow:
    def __init__(self) -> None:
        self.received_signal = False
        self.main_workflow_coroutine_finished = False

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self.received_signal)
        self.main_workflow_coroutine_finished = True
        return "workflow-result"

    @workflow.signal(unfinished_policy=workflow.HandlerUnfinishedPolicy.ABANDON)
    async def my_signal(self):
        self.received_signal = True
        await workflow.wait_condition(lambda: self.main_workflow_coroutine_finished)
        await asyncio.sleep(7777777)


async def test_timer_started_after_workflow_completion(client: Client):
    async with new_worker(
        client, TimerStartedAfterWorkflowCompletionWorkflow
    ) as worker:
        handle = await client.start_workflow(
            TimerStartedAfterWorkflowCompletionWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(TimerStartedAfterWorkflowCompletionWorkflow.my_signal)
        assert await handle.result() == "workflow-result"


@activity.defn
async def activity_with_retry_delay():
    raise ApplicationError(
        ActivitiesWithRetryDelayWorkflow.error_message,
        next_retry_delay=ActivitiesWithRetryDelayWorkflow.next_retry_delay,
    )


@workflow.defn
class ActivitiesWithRetryDelayWorkflow:
    error_message = "Deliberately failing with next_retry_delay set"
    next_retry_delay = timedelta(milliseconds=5)

    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            activity_with_retry_delay,
            retry_policy=RetryPolicy(maximum_attempts=2),
            schedule_to_close_timeout=timedelta(minutes=5),
        )


async def test_activity_retry_delay(client: Client):
    async with new_worker(
        client, ActivitiesWithRetryDelayWorkflow, activities=[activity_with_retry_delay]
    ) as worker:
        try:
            await client.execute_workflow(
                ActivitiesWithRetryDelayWorkflow.run,
                id=str(uuid.uuid4()),
                task_queue=worker.task_queue,
            )
        except WorkflowFailureError as err:
            assert isinstance(err.cause, ActivityError)
            assert isinstance(err.cause.cause, ApplicationError)
            assert (
                str(err.cause.cause) == ActivitiesWithRetryDelayWorkflow.error_message
            )
            assert (
                err.cause.cause.next_retry_delay
                == ActivitiesWithRetryDelayWorkflow.next_retry_delay
            )


@workflow.defn
class WorkflowWithoutInit:
    value = "from class attribute"
    _expected_update_result = "from class attribute"

    @workflow.update
    async def my_update(self) -> str:
        return self.value

    @workflow.run
    async def run(self, _: str) -> str:
        self.value = "set in run method"
        return self.value


@workflow.defn
class WorkflowWithWorkflowInit:
    _expected_update_result = "workflow input value"

    @workflow.init
    def __init__(self, arg: str) -> None:
        self.value = arg

    @workflow.update
    async def my_update(self) -> str:
        return self.value

    @workflow.run
    async def run(self, _: str) -> str:
        self.value = "set in run method"
        return self.value


@workflow.defn
class WorkflowWithNonWorkflowInitInit:
    _expected_update_result = "from parameter default"

    def __init__(self, arg: str = "from parameter default") -> None:
        self.value = arg

    @workflow.update
    async def my_update(self) -> str:
        return self.value

    @workflow.run
    async def run(self, _: str) -> str:
        self.value = "set in run method"
        return self.value


@pytest.mark.parametrize(
    ["client_cls", "worker_cls"],
    [
        (WorkflowWithoutInit, WorkflowWithoutInit),
        (WorkflowWithNonWorkflowInitInit, WorkflowWithNonWorkflowInitInit),
        (WorkflowWithWorkflowInit, WorkflowWithWorkflowInit),
    ],
)
async def test_update_in_first_wft_sees_workflow_init(
    client: Client, client_cls: Type, worker_cls: Type
):
    """
    Test how @workflow.init affects what an update in the first WFT sees.

    Such an update is guaranteed to start executing before the main workflow
    coroutine. The update should see the side effects of the __init__ method if
    and only if @workflow.init is in effect.
    """
    # This test must ensure that the update is in the first WFT. To do so,
    # before running the worker, we start the workflow, send the update, and
    # wait until the update is admitted.
    task_queue = "task-queue"
    update_id = "update-id"
    wf_handle = await client.start_workflow(
        client_cls.run,
        "workflow input value",
        id=str(uuid.uuid4()),
        task_queue=task_queue,
    )
    update_task = asyncio.create_task(
        wf_handle.execute_update(client_cls.my_update, id=update_id)
    )
    await assert_eq_eventually(
        True, lambda: workflow_update_exists(client, wf_handle.id, update_id)
    )
    # When the worker starts polling it will receive a first WFT containing the
    # update, in addition to the start_workflow job.
    async with new_worker(client, worker_cls, task_queue=task_queue):
        assert await update_task == worker_cls._expected_update_result
        assert await wf_handle.result() == "set in run method"


@workflow.defn
class WorkflowRunSeesWorkflowInitWorkflow:
    @workflow.init
    def __init__(self, arg: str) -> None:
        self.value = arg

    @workflow.run
    async def run(self, _: str):
        return f"hello, {self.value}"


async def test_workflow_run_sees_workflow_init(client: Client):
    async with new_worker(client, WorkflowRunSeesWorkflowInitWorkflow) as worker:
        workflow_result = await client.execute_workflow(
            WorkflowRunSeesWorkflowInitWorkflow.run,
            "world",
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        assert workflow_result == "hello, world"


@workflow.defn
class UserMetadataWorkflow:
    def __init__(self) -> None:
        self._done = False
        self._waiting = False

    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            say_hello,
            "Enchi",
            start_to_close_timeout=timedelta(seconds=5),
            summary="meow",
        )
        # Force timeout, ignore, wait again
        try:
            await workflow.wait_condition(
                lambda: self._done, timeout=0.01, timeout_summary="hi!"
            )
            raise RuntimeError("Expected timeout")
        except asyncio.TimeoutError:
            pass
        await workflow.sleep(0.01, summary="timer2")
        self._waiting = True
        workflow.set_current_details("such detail")
        await workflow.wait_condition(lambda: self._done)

    @workflow.signal(description="sdesc")
    def done(self) -> None:
        self._done = True

    @workflow.query(description="qdesc")
    def waiting(self) -> bool:
        return self._waiting

    @workflow.update(description="udesc")
    def some_update(self):
        pass


async def test_user_metadata_is_set(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2219"
        )
    async with new_worker(
        client, UserMetadataWorkflow, activities=[say_hello]
    ) as worker:
        handle = await client.start_workflow(
            UserMetadataWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            static_summary="cool workflow bro",
            static_details="xtremely detailed",
        )

        # Wait until it's waiting, then send the signal
        async def waiting() -> bool:
            return await handle.query(UserMetadataWorkflow.waiting)

        await assert_eq_eventually(True, waiting)

        md_query: temporalio.api.sdk.v1.WorkflowMetadata = await handle.query(
            "__temporal_workflow_metadata",
            result_type=temporalio.api.sdk.v1.WorkflowMetadata,
        )
        matched_q = [
            q for q in md_query.definition.query_definitions if q.name == "waiting"
        ]
        assert len(matched_q) == 1
        assert matched_q[0].description == "qdesc"

        matched_u = [
            u for u in md_query.definition.update_definitions if u.name == "some_update"
        ]
        assert len(matched_u) == 1
        assert matched_u[0].description == "udesc"

        matched_s = [
            s for s in md_query.definition.signal_definitions if s.name == "done"
        ]
        assert len(matched_s) == 1
        assert matched_s[0].description == "sdesc"

        assert md_query.current_details == "such detail"

        await handle.signal(UserMetadataWorkflow.done)
        await handle.result()

        # Ensure metadatas are present in history
        resp = await client.workflow_service.get_workflow_execution_history(
            GetWorkflowExecutionHistoryRequest(
                namespace=client.namespace,
                execution=WorkflowExecution(workflow_id=handle.id),
            )
        )
        timer_summs = set()
        for event in resp.history.events:
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
                assert "cool workflow bro" in PayloadConverter.default.from_payload(
                    event.user_metadata.summary
                )
                assert "xtremely detailed" in PayloadConverter.default.from_payload(
                    event.user_metadata.details
                )
            elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                assert "meow" in PayloadConverter.default.from_payload(
                    event.user_metadata.summary
                )
            elif event.event_type == EventType.EVENT_TYPE_TIMER_STARTED:
                timer_summs.add(
                    PayloadConverter.default.from_payload(event.user_metadata.summary)
                )
        assert timer_summs == {"hi!", "timer2"}

        describe_r = await handle.describe()
        assert describe_r.static_summary == "cool workflow bro"
        assert describe_r.static_details == "xtremely detailed"


@workflow.defn
class WorkflowSleepWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.sleep(1)


async def test_workflow_sleep(client: Client):
    async with new_worker(client, WorkflowSleepWorkflow) as worker:
        start_time = datetime.now()
        await client.execute_workflow(
            WorkflowSleepWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert (datetime.now() - start_time) >= timedelta(seconds=1)


@workflow.defn
class ConcurrentSleepsWorkflow:
    @workflow.run
    async def run(self) -> None:
        sleeps_a = [workflow.sleep(0.1, summary=f"t{i}") for i in range(5)]
        zero_a = workflow.sleep(0, summary="zero_timer")
        wait_some = workflow.wait_condition(
            lambda: False, timeout=0.1, timeout_summary="wait_some"
        )
        zero_b = workflow.wait_condition(
            lambda: False, timeout=0, timeout_summary="zero_wait"
        )
        no_summ = workflow.sleep(0.1)
        sleeps_b = [workflow.sleep(0.1, summary=f"t{i}") for i in range(5, 10)]
        try:
            await asyncio.gather(
                *sleeps_a,
                zero_a,
                wait_some,
                zero_b,
                no_summ,
                *sleeps_b,
                return_exceptions=True,
            )
        except asyncio.TimeoutError:
            pass

        task_1 = asyncio.create_task(self.make_timers(100, 105))
        task_2 = asyncio.create_task(self.make_timers(105, 110))
        await asyncio.gather(task_1, task_2)

    async def make_timers(self, start: int, end: int):
        await asyncio.gather(
            *[workflow.sleep(0.1, summary=f"m_t{i}") for i in range(start, end)]
        )


async def test_concurrent_sleeps_use_proper_options(
    client: Client, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/2219"
        )
    async with new_worker(client, ConcurrentSleepsWorkflow) as worker:
        handle = await client.start_workflow(
            ConcurrentSleepsWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
        resp = await client.workflow_service.get_workflow_execution_history(
            GetWorkflowExecutionHistoryRequest(
                namespace=client.namespace,
                execution=WorkflowExecution(workflow_id=handle.id),
            )
        )
        timer_summaries = [
            PayloadConverter.default.from_payload(e.user_metadata.summary)
            if e.user_metadata.HasField("summary")
            else "<no summ>"
            for e in resp.history.events
            if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
        ]
        assert timer_summaries == [
            *[f"t{i}" for i in range(5)],
            "zero_timer",
            "wait_some",
            "<no summ>",
            *[f"t{i}" for i in range(5, 10)],
            *[f"m_t{i}" for i in range(100, 110)],
        ]

        # Force replay with a query to ensure determinism
        await handle.query("__temporal_workflow_metadata")


class BadFailureConverterError(Exception):
    pass


class BadFailureConverter(DefaultFailureConverter):
    def to_failure(
        self,
        exception: BaseException,
        payload_converter: PayloadConverter,
        failure: Failure,
    ) -> None:
        if isinstance(exception, BadFailureConverterError):
            raise RuntimeError("Intentional failure conversion error")
        super().to_failure(exception, payload_converter, failure)


@activity.defn
async def bad_failure_converter_activity() -> None:
    raise BadFailureConverterError


@workflow.defn(sandboxed=False)
class BadFailureConverterWorkflow:
    @workflow.run
    async def run(self, fail_workflow_task) -> None:
        if fail_workflow_task:
            raise BadFailureConverterError
        else:
            await workflow.execute_activity(
                bad_failure_converter_activity,
                schedule_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )


async def test_bad_failure_converter(client: Client):
    config = client.config()
    config["data_converter"] = dataclasses.replace(
        config["data_converter"],
        failure_converter_class=BadFailureConverter,
    )
    client = Client(**config)
    async with new_worker(
        client, BadFailureConverterWorkflow, activities=[bad_failure_converter_activity]
    ) as worker:
        # Check activity
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                BadFailureConverterWorkflow.run,
                False,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert isinstance(err.value.cause, ActivityError)
        assert isinstance(err.value.cause.cause, ApplicationError)
        assert (
            err.value.cause.cause.message
            == "Failed building exception result: Intentional failure conversion error"
        )

        # Check workflow
        handle = await client.start_workflow(
            BadFailureConverterWorkflow.run,
            True,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        async def task_failed_message() -> Optional[str]:
            async for e in handle.fetch_history_events():
                if e.HasField("workflow_task_failed_event_attributes"):
                    return e.workflow_task_failed_event_attributes.failure.message
            return None

        await assert_eq_eventually(
            "Failed converting activation exception: Intentional failure conversion error",
            task_failed_message,  # type: ignore
        )


@workflow.defn
class SignalsActivitiesTimersUpdatesTracingWorkflow:
    """
    These handlers all do different things that will cause the event loop to yield, sometimes
    until the next workflow task (ex: timer) sometimes within the workflow task (ex: future resolve
    or wait condition).
    """

    def __init__(self) -> None:
        self.events: List[str] = []

    @workflow.run
    async def run(self) -> List[str]:
        tt = asyncio.create_task(self.run_timer())
        at = asyncio.create_task(self.run_act())
        await asyncio.gather(tt, at)
        return self.events

    @workflow.signal
    async def dosig(self, name: str):
        self.events.append(f"sig-{name}-sync")
        fut: asyncio.Future[bool] = asyncio.Future()
        fut.set_result(True)
        await fut
        self.events.append(f"sig-{name}-1")
        await workflow.wait_condition(lambda: True)
        self.events.append(f"sig-{name}-2")

    @workflow.update
    async def doupdate(self, name: str):
        self.events.append(f"update-{name}-sync")
        fut: asyncio.Future[bool] = asyncio.Future()
        fut.set_result(True)
        await fut
        self.events.append(f"update-{name}-1")
        await workflow.wait_condition(lambda: True)
        self.events.append(f"update-{name}-2")

    async def run_timer(self):
        self.events.append("timer-sync")
        await workflow.sleep(0.1)
        fut: asyncio.Future[bool] = asyncio.Future()
        fut.set_result(True)
        await fut
        self.events.append("timer-1")
        await workflow.wait_condition(lambda: True)
        self.events.append("timer-2")

    async def run_act(self):
        self.events.append("act-sync")
        await workflow.execute_activity(
            say_hello, "Enchi", schedule_to_close_timeout=timedelta(seconds=30)
        )
        fut: asyncio.Future[bool] = asyncio.Future()
        fut.set_result(True)
        await fut
        self.events.append("act-1")
        await workflow.wait_condition(lambda: True)
        self.events.append("act-2")


async def test_async_loop_ordering(client: Client, env: WorkflowEnvironment):
    """This test mostly exists to generate histories for test_replayer_async_ordering.
    See that test for more."""

    if env.supports_time_skipping:
        pytest.skip("This test doesn't work right with time skipping for some reason")
    task_queue = f"tq-{uuid.uuid4()}"
    handle = await client.start_workflow(
        SignalsActivitiesTimersUpdatesTracingWorkflow.run,
        id=f"wf-{uuid.uuid4()}",
        task_queue=task_queue,
    )
    await handle.signal(SignalsActivitiesTimersUpdatesTracingWorkflow.dosig, "before")

    async with new_worker(
        client,
        SignalsActivitiesTimersUpdatesTracingWorkflow,
        activities=[say_hello],
        task_queue=task_queue,
    ):
        await asyncio.sleep(0.2)
        await handle.signal(SignalsActivitiesTimersUpdatesTracingWorkflow.dosig, "1")
        await handle.execute_update(
            SignalsActivitiesTimersUpdatesTracingWorkflow.doupdate, "1"
        )
        await handle.result()


@workflow.defn
class ActivityAndSignalsWhileWorkflowDown:
    def __init__(self) -> None:
        self.events: List[str] = []
        self.counter = 0

    @workflow.run
    async def run(self, activity_tq: str) -> List[str]:
        act_task = asyncio.create_task(self.run_act(activity_tq))
        await workflow.wait_condition(lambda: self.counter >= 2)
        self.events.append(f"counter-{self.counter}")
        await act_task
        return self.events

    @workflow.signal
    async def dosig(self, name: str):
        self.events.append(f"sig-{name}")
        self.counter += 1

    async def run_act(self, activity_tq: str):
        self.events.append("act-start")
        await workflow.execute_activity(
            say_hello,
            "Enchi",
            schedule_to_close_timeout=timedelta(seconds=30),
            task_queue=activity_tq,
        )
        self.counter += 1
        self.events.append("act-done")


async def test_alternate_async_loop_ordering(client: Client, env: WorkflowEnvironment):
    """This test mostly exists to generate histories for test_replayer_alternate_async_ordering.
    See that test for more."""

    if env.supports_time_skipping:
        pytest.skip("This test doesn't work right with time skipping for some reason")
    task_queue = f"tq-{uuid.uuid4()}"
    activity_tq = f"tq-{uuid.uuid4()}"
    handle = await client.start_workflow(
        ActivityAndSignalsWhileWorkflowDown.run,
        activity_tq,
        id=f"wf-{uuid.uuid4()}",
        task_queue=task_queue,
    )

    async with new_worker(
        client,
        ActivityAndSignalsWhileWorkflowDown,
        activities=[say_hello],
        task_queue=task_queue,
    ):
        # This sleep exists to make sure the first WFT is processed
        await asyncio.sleep(0.2)

    async with new_worker(
        client,
        activities=[say_hello],
        task_queue=activity_tq,
    ):
        # Make sure the activity starts being processed before sending signals
        await asyncio.sleep(1)
        await handle.signal(ActivityAndSignalsWhileWorkflowDown.dosig, "1")
        await handle.signal(ActivityAndSignalsWhileWorkflowDown.dosig, "2")

        async with new_worker(
            client,
            ActivityAndSignalsWhileWorkflowDown,
            activities=[say_hello],
            task_queue=task_queue,
        ):
            await handle.result()


# The following Lock and Semaphore tests test that asyncio concurrency primitives work as expected
# in workflow code. There is nothing Temporal-specific about the way that asyncio.Lock and
# asyncio.Semaphore are used here.


@activity.defn
async def noop_activity_for_lock_or_semaphore_tests() -> None:
    return None


@dataclass
class LockOrSemaphoreWorkflowConcurrencySummary:
    ever_in_critical_section: int
    peak_in_critical_section: int


@dataclass
class UseLockOrSemaphoreWorkflowParameters:
    n_coroutines: int = 0
    semaphore_initial_value: Optional[int] = None
    sleep: Optional[float] = None
    timeout: Optional[float] = None


@workflow.defn
class CoroutinesUseLockOrSemaphoreWorkflow:
    def __init__(self) -> None:
        self.params: UseLockOrSemaphoreWorkflowParameters
        self.lock_or_semaphore: Union[asyncio.Lock, asyncio.Semaphore]
        self._currently_in_critical_section: set[str] = set()
        self._ever_in_critical_section: set[str] = set()
        self._peak_in_critical_section = 0

    def init(self, params: UseLockOrSemaphoreWorkflowParameters):
        self.params = params
        if self.params.semaphore_initial_value is not None:
            self.lock_or_semaphore = asyncio.Semaphore(
                self.params.semaphore_initial_value
            )
        else:
            self.lock_or_semaphore = asyncio.Lock()

    @workflow.run
    async def run(
        self,
        params: Optional[UseLockOrSemaphoreWorkflowParameters],
    ) -> LockOrSemaphoreWorkflowConcurrencySummary:
        # TODO: Use workflow init method when it exists.
        assert params
        self.init(params)
        await asyncio.gather(
            *(self.coroutine(f"{i}") for i in range(self.params.n_coroutines))
        )
        assert not any(self._currently_in_critical_section)
        return LockOrSemaphoreWorkflowConcurrencySummary(
            len(self._ever_in_critical_section),
            self._peak_in_critical_section,
        )

    async def coroutine(self, id: str):
        if self.params.timeout:
            try:
                await asyncio.wait_for(
                    self.lock_or_semaphore.acquire(), self.params.timeout
                )
            except asyncio.TimeoutError:
                return
        else:
            await self.lock_or_semaphore.acquire()
        self._enters_critical_section(id)
        try:
            if self.params.sleep:
                await asyncio.sleep(self.params.sleep)
            else:
                await workflow.execute_activity(
                    noop_activity_for_lock_or_semaphore_tests,
                    schedule_to_close_timeout=timedelta(seconds=30),
                )
        finally:
            self.lock_or_semaphore.release()
            self._exits_critical_section(id)

    def _enters_critical_section(self, id: str) -> None:
        self._currently_in_critical_section.add(id)
        self._ever_in_critical_section.add(id)
        self._peak_in_critical_section = max(
            self._peak_in_critical_section,
            len(self._currently_in_critical_section),
        )

    def _exits_critical_section(self, id: str) -> None:
        self._currently_in_critical_section.remove(id)


@workflow.defn
class HandlerCoroutinesUseLockOrSemaphoreWorkflow(CoroutinesUseLockOrSemaphoreWorkflow):
    def __init__(self) -> None:
        super().__init__()
        self.workflow_may_exit = False

    @workflow.run
    async def run(
        self,
        _: Optional[UseLockOrSemaphoreWorkflowParameters] = None,
    ) -> LockOrSemaphoreWorkflowConcurrencySummary:
        await workflow.wait_condition(lambda: self.workflow_may_exit)
        return LockOrSemaphoreWorkflowConcurrencySummary(
            len(self._ever_in_critical_section),
            self._peak_in_critical_section,
        )

    @workflow.update
    async def my_update(self, params: UseLockOrSemaphoreWorkflowParameters):
        # TODO: Use workflow init method when it exists.
        if not hasattr(self, "params"):
            self.init(params)
        assert (update_info := workflow.current_update_info())
        await self.coroutine(update_info.id)

    @workflow.signal
    async def finish(self):
        self.workflow_may_exit = True


async def _do_workflow_coroutines_lock_or_semaphore_test(
    client: Client,
    params: UseLockOrSemaphoreWorkflowParameters,
    expectation: LockOrSemaphoreWorkflowConcurrencySummary,
):
    async with new_worker(
        client,
        CoroutinesUseLockOrSemaphoreWorkflow,
        activities=[noop_activity_for_lock_or_semaphore_tests],
    ) as worker:
        summary = await client.execute_workflow(
            CoroutinesUseLockOrSemaphoreWorkflow.run,
            arg=params,
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        assert summary == expectation


async def _do_update_handler_lock_or_semaphore_test(
    client: Client,
    env: WorkflowEnvironment,
    params: UseLockOrSemaphoreWorkflowParameters,
    n_updates: int,
    expectation: LockOrSemaphoreWorkflowConcurrencySummary,
):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1903"
        )

    task_queue = "tq"
    handle = await client.start_workflow(
        HandlerCoroutinesUseLockOrSemaphoreWorkflow.run,
        id=f"wf-{str(uuid.uuid4())}",
        task_queue=task_queue,
    )
    # Create updates in Admitted state, before the worker starts polling.
    admitted_updates = [
        await admitted_update_task(
            client,
            handle,
            HandlerCoroutinesUseLockOrSemaphoreWorkflow.my_update,
            arg=params,
            id=f"update-{i}",
        )
        for i in range(n_updates)
    ]
    async with new_worker(
        client,
        HandlerCoroutinesUseLockOrSemaphoreWorkflow,
        activities=[noop_activity_for_lock_or_semaphore_tests],
        task_queue=task_queue,
    ):
        for update_task in admitted_updates:
            await update_task
        await handle.signal(HandlerCoroutinesUseLockOrSemaphoreWorkflow.finish)
        summary = await handle.result()
        assert summary == expectation


async def test_workflow_coroutines_can_use_lock(client: Client):
    await _do_workflow_coroutines_lock_or_semaphore_test(
        client,
        UseLockOrSemaphoreWorkflowParameters(n_coroutines=5),
        # The lock limits concurrency to 1
        expectation=LockOrSemaphoreWorkflowConcurrencySummary(
            ever_in_critical_section=5, peak_in_critical_section=1
        ),
    )


async def test_update_handler_can_use_lock_to_serialize_handler_executions(
    client: Client, env: WorkflowEnvironment
):
    await _do_update_handler_lock_or_semaphore_test(
        client,
        env,
        UseLockOrSemaphoreWorkflowParameters(),
        n_updates=5,
        # The lock limits concurrency to 1
        expectation=LockOrSemaphoreWorkflowConcurrencySummary(
            ever_in_critical_section=5, peak_in_critical_section=1
        ),
    )


async def test_workflow_coroutines_lock_acquisition_respects_timeout(client: Client):
    await _do_workflow_coroutines_lock_or_semaphore_test(
        client,
        UseLockOrSemaphoreWorkflowParameters(n_coroutines=5, sleep=0.5, timeout=0.1),
        # Second and subsequent coroutines fail to acquire the lock due to the timeout.
        expectation=LockOrSemaphoreWorkflowConcurrencySummary(
            ever_in_critical_section=1, peak_in_critical_section=1
        ),
    )


async def test_update_handler_lock_acquisition_respects_timeout(
    client: Client, env: WorkflowEnvironment
):
    await _do_update_handler_lock_or_semaphore_test(
        client,
        env,
        # Second and subsequent handler executions fail to acquire the lock due to the timeout.
        UseLockOrSemaphoreWorkflowParameters(sleep=0.5, timeout=0.1),
        n_updates=5,
        expectation=LockOrSemaphoreWorkflowConcurrencySummary(
            ever_in_critical_section=1, peak_in_critical_section=1
        ),
    )


async def test_workflow_coroutines_can_use_semaphore(client: Client):
    await _do_workflow_coroutines_lock_or_semaphore_test(
        client,
        UseLockOrSemaphoreWorkflowParameters(n_coroutines=5, semaphore_initial_value=3),
        # The semaphore limits concurrency to 3
        expectation=LockOrSemaphoreWorkflowConcurrencySummary(
            ever_in_critical_section=5, peak_in_critical_section=3
        ),
    )


async def test_update_handler_can_use_semaphore_to_control_handler_execution_concurrency(
    client: Client, env: WorkflowEnvironment
):
    await _do_update_handler_lock_or_semaphore_test(
        client,
        env,
        # The semaphore limits concurrency to 3
        UseLockOrSemaphoreWorkflowParameters(semaphore_initial_value=3),
        n_updates=5,
        expectation=LockOrSemaphoreWorkflowConcurrencySummary(
            ever_in_critical_section=5, peak_in_critical_section=3
        ),
    )


async def test_workflow_coroutine_semaphore_acquisition_respects_timeout(
    client: Client,
):
    await _do_workflow_coroutines_lock_or_semaphore_test(
        client,
        UseLockOrSemaphoreWorkflowParameters(
            n_coroutines=5, semaphore_initial_value=3, sleep=0.5, timeout=0.1
        ),
        # Initial entry to the semaphore succeeds, but all subsequent attempts to acquire a semaphore
        # slot fail.
        expectation=LockOrSemaphoreWorkflowConcurrencySummary(
            ever_in_critical_section=3, peak_in_critical_section=3
        ),
    )


async def test_update_handler_semaphore_acquisition_respects_timeout(
    client: Client, env: WorkflowEnvironment
):
    await _do_update_handler_lock_or_semaphore_test(
        client,
        env,
        # Initial entry to the semaphore succeeds, but all subsequent attempts to acquire a semaphore
        # slot fail.
        UseLockOrSemaphoreWorkflowParameters(
            semaphore_initial_value=3,
            sleep=0.5,
            timeout=0.1,
        ),
        n_updates=5,
        expectation=LockOrSemaphoreWorkflowConcurrencySummary(
            ever_in_critical_section=3, peak_in_critical_section=3
        ),
    )
