import asyncio
import dataclasses
import json
import logging
import logging.handlers
import pickle
import queue
import sys
import threading
import uuid
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Awaitable,
    Dict,
    List,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    Tuple,
    cast,
)

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from typing_extensions import Protocol, runtime_checkable

from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload, Payloads, WorkflowExecution
from temporalio.api.enums.v1 import EventType, IndexedValueType
from temporalio.api.failure.v1 import Failure
from temporalio.api.operatorservice.v1 import (
    AddSearchAttributesRequest,
    ListSearchAttributesRequest,
)
from temporalio.api.workflowservice.v1 import GetWorkflowExecutionHistoryRequest
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
)
from temporalio.common import RawValue, RetryPolicy, SearchAttributes
from temporalio.converter import (
    DataConverter,
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
    TimeoutError,
    WorkflowAlreadyStartedError,
)
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    UnsandboxedWorkflowRunner,
    Worker,
    WorkflowInstance,
    WorkflowInstanceDetails,
    WorkflowRunner,
)
from tests.helpers import assert_eq_eventually, new_worker


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
        ret["current_history_length"] = workflow.info().get_current_history_length()
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
        assert info["current_history_length"] == 3
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
            " known queries: [__stack_trace bad_query other_query]"
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
        with pytest.raises(WorkflowFailureError) as err:
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
                        LongSleepWorkflow.run, workflow_id=f"{handle.id}_child"  # type: ignore[arg-type]
                    ).query(
                        LongSleepWorkflow.started
                    )
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
        handle: workflow.ExternalWorkflowHandle[
            ReturnSignalWorkflow
        ] = workflow.get_external_workflow_handle_for(
            ReturnSignalWorkflow.run, args.external_workflow_id
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


async def test_workflow_with_codec(client: Client):
    # Make client with this codec and run a couple of existing tests
    config = client.config()
    config["data_converter"] = DataConverter(payload_codec=SimpleCodec())
    client = Client(**config)
    await test_workflow_signal_and_query(client)
    await test_workflow_signal_and_query_errors(client)
    await test_workflow_simple_activity(client)


class CustomWorkflowRunner(WorkflowRunner):
    def __init__(self) -> None:
        super().__init__()
        self._unsandboxed = UnsandboxedWorkflowRunner()
        self._pairs: List[Tuple[WorkflowActivation, WorkflowActivationCompletion]] = []

    def prepare_workflow(self, defn: workflow._Definition) -> None:
        pass

    def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        # We need to assert details can be pickled for potential sandbox use
        det_pickled = pickle.loads(pickle.dumps(det))
        assert det == det_pickled
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
    # Confirm first activation and last completion
    assert runner._pairs[0][0].jobs[0].start_workflow.workflow_type == "HelloWorkflow"
    assert (
        runner._pairs[-1][-1]
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


def search_attrs_to_dict_with_type(attrs: SearchAttributes) -> Mapping[str, Any]:
    return {
        k: {
            "type": vals[0].__class__.__name__ if vals else "<unknown>",
            "values": [str(v) if isinstance(v, datetime) else v for v in vals],
        }
        for k, vals in attrs.items()
    }


@workflow.defn
class SearchAttributeWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Wait forever
        await asyncio.Future()

    @workflow.query
    def get_search_attributes(self) -> Mapping[str, Mapping[str, Any]]:
        return search_attrs_to_dict_with_type(workflow.info().search_attributes or {})

    @workflow.signal
    def do_search_attribute_update(self) -> None:
        empty_float_list: List[float] = []
        workflow.upsert_search_attributes(
            {
                f"{sa_prefix}text": ["text2"],
                # We intentionally leave keyword off to confirm it still comes
                # back but replace keyword list
                f"{sa_prefix}keyword_list": ["keywordlist3", "keywordlist4"],
                f"{sa_prefix}int": [456],
                # Empty list to confirm removed
                f"{sa_prefix}double": empty_float_list,
                f"{sa_prefix}bool": [False],
                f"{sa_prefix}datetime": [
                    datetime(2003, 4, 5, 6, 7, 8, tzinfo=timezone(timedelta(hours=9)))
                ],
            }
        )


async def test_workflow_search_attributes(client: Client, env_type: str):
    if env_type != "local":
        pytest.skip("Only testing search attributes on local which disables cache")

    async def search_attributes_present() -> bool:
        resp = await client.operator_service.list_search_attributes(
            ListSearchAttributesRequest(namespace=client.namespace)
        )
        return any(k for k in resp.custom_attributes.keys() if k.startswith(sa_prefix))

    # Add search attributes if not already present
    if not await search_attributes_present():
        await client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(
                namespace=client.namespace,
                search_attributes={
                    f"{sa_prefix}text": IndexedValueType.INDEXED_VALUE_TYPE_TEXT,
                    f"{sa_prefix}keyword": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
                    f"{sa_prefix}keyword_list": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD_LIST,
                    f"{sa_prefix}int": IndexedValueType.INDEXED_VALUE_TYPE_INT,
                    f"{sa_prefix}double": IndexedValueType.INDEXED_VALUE_TYPE_DOUBLE,
                    f"{sa_prefix}bool": IndexedValueType.INDEXED_VALUE_TYPE_BOOL,
                    f"{sa_prefix}datetime": IndexedValueType.INDEXED_VALUE_TYPE_DATETIME,
                },
            ),
        )
    # Confirm now present
    assert await search_attributes_present()

    async with new_worker(client, SearchAttributeWorkflow) as worker:
        handle = await client.start_workflow(
            SearchAttributeWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            search_attributes={
                f"{sa_prefix}text": ["text1"],
                f"{sa_prefix}keyword": ["keyword1"],
                f"{sa_prefix}keyword_list": ["keywordlist1", "keywordlist2"],
                f"{sa_prefix}int": [123],
                f"{sa_prefix}double": [456.78],
                f"{sa_prefix}bool": [True],
                f"{sa_prefix}datetime": [
                    datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
                ],
            },
        )
        # Make sure it started with the right attributes
        expected = {
            f"{sa_prefix}text": {"type": "str", "values": ["text1"]},
            f"{sa_prefix}keyword": {"type": "str", "values": ["keyword1"]},
            f"{sa_prefix}keyword_list": {
                "type": "str",
                "values": ["keywordlist1", "keywordlist2"],
            },
            f"{sa_prefix}int": {"type": "int", "values": [123]},
            f"{sa_prefix}double": {"type": "float", "values": [456.78]},
            f"{sa_prefix}bool": {"type": "bool", "values": [True]},
            f"{sa_prefix}datetime": {
                "type": "datetime",
                "values": ["2001-02-03 04:05:06+00:00"],
            },
        }
        assert expected == await handle.query(
            SearchAttributeWorkflow.get_search_attributes
        )

        # Do an attribute update and check query
        await handle.signal(SearchAttributeWorkflow.do_search_attribute_update)
        expected = {
            f"{sa_prefix}text": {"type": "str", "values": ["text2"]},
            f"{sa_prefix}keyword": {"type": "str", "values": ["keyword1"]},
            f"{sa_prefix}keyword_list": {
                "type": "str",
                "values": ["keywordlist3", "keywordlist4"],
            },
            f"{sa_prefix}int": {"type": "int", "values": [456]},
            f"{sa_prefix}double": {"type": "<unknown>", "values": []},
            f"{sa_prefix}bool": {"type": "bool", "values": [False]},
            f"{sa_prefix}datetime": {
                "type": "datetime",
                "values": ["2003-04-05 06:07:08+09:00"],
            },
        }
        assert expected == await handle.query(
            SearchAttributeWorkflow.get_search_attributes
        )

        # Also confirm it matches describe from the server
        desc = await handle.describe()
        # Remove attrs without our prefix
        attrs = {
            k: v for k, v in desc.search_attributes.items() if k.startswith(sa_prefix)
        }
        # Check against expected, but remove double from expected since it is
        # no longer present
        del expected[f"{sa_prefix}double"]
        assert expected == search_attrs_to_dict_with_type(attrs)


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

    @workflow.query
    def last_signal(self) -> str:
        return self._last_signal


async def test_workflow_logging(client: Client, env: WorkflowEnvironment):
    # Use queue to capture log statements
    log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
    handler = logging.handlers.QueueHandler(log_queue)
    workflow.logger.base_logger.addHandler(handler)
    prev_level = workflow.logger.base_logger.level
    workflow.logger.base_logger.setLevel(logging.INFO)

    def find_log(starts_with: str) -> Optional[logging.LogRecord]:
        for record in cast(List[logging.LogRecord], log_queue.queue):
            if record.message.startswith(starts_with):
                return record
        return None

    try:
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
            # Send a couple signals
            await handle.signal(LoggingWorkflow.my_signal, "signal 1")
            await handle.signal(LoggingWorkflow.my_signal, "signal 2")
            assert "signal 2" == await handle.query(LoggingWorkflow.last_signal)

        # Confirm two logs happened
        assert find_log("Signal: signal 1 ({'attempt':")
        assert find_log("Signal: signal 2")
        assert not find_log("Signal: signal 3")
        # Also make sure it has some workflow info
        record = find_log("Signal: signal 1")
        assert (
            record
            and record.__dict__["workflow_info"].workflow_type == "LoggingWorkflow"
        )

        # Clear queue and start a new one with more signals
        log_queue.queue.clear()
        async with new_worker(
            client,
            LoggingWorkflow,
            task_queue=worker.task_queue,
            max_cached_workflows=0,
        ) as worker:
            # Send a couple signals
            await handle.signal(LoggingWorkflow.my_signal, "signal 3")
            await handle.signal(LoggingWorkflow.my_signal, "finish")
            await handle.result()

        # Confirm replayed logs are not present but new ones are
        assert not find_log("Signal: signal 1")
        assert not find_log("Signal: signal 2")
        assert find_log("Signal: signal 3")
        assert find_log("Signal: finish")
    finally:
        workflow.logger.base_logger.removeHandler(handler)
        workflow.logger.base_logger.setLevel(prev_level)


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
        await asyncio.wait([asyncio.create_task(v) for v in awaitables])

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
    async def run(self, arg: MyDataClass) -> MyDataClass:
        ...

    @workflow.signal
    def signal_sync(self, param: MyDataClass) -> None:
        ...

    @workflow.query
    def query_sync(self, param: MyDataClass) -> MyDataClass:
        ...

    @workflow.signal
    def complete(self) -> None:
        ...


@workflow.defn(name="DataClassTypedWorkflow")
class DataClassTypedWorkflowAbstract(ABC):
    @workflow.run
    @abstractmethod
    async def run(self, arg: MyDataClass) -> MyDataClass:
        ...

    @workflow.signal
    @abstractmethod
    def signal_sync(self, param: MyDataClass) -> None:
        ...

    @workflow.query
    @abstractmethod
    def query_sync(self, param: MyDataClass) -> MyDataClass:
        ...

    @workflow.signal
    @abstractmethod
    def complete(self) -> None:
        ...


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
        # Our decorators add attributes on the class, but protocols don't allow
        # you to use issubclass with any attributes other than their fixed ones.
        # We are asserting that this invariant holds so we can document it and
        # revisit in a later version if they change this.
        # TODO(cretz): If we document how to use protocols as workflow
        # interfaces/contracts, we should mention that they can't use
        # @runtime_checkable with issubclass.
        with pytest.raises(TypeError) as err:
            assert issubclass(DataClassTypedWorkflow, DataClassTypedWorkflowProto)
        assert "non-method members" in str(err.value)

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
        with pytest.raises(WorkflowAlreadyStartedError):
            await client.start_workflow(
                LongSleepWorkflow.run,
                id=id,
                task_queue=worker.task_queue,
            )
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
    async with new_worker(client, DeadlockedWorkflow) as worker:
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
                "Potential deadlock detected, workflow didn't yield within 2 second(s)",
                last_history_task_failure,
                timeout=timedelta(seconds=5),
                interval=timedelta(seconds=1),
            )
        finally:
            deadlock_thread_event.set()


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
    # TODO(cretz): Patches have issues on older servers since core needs patch
    # metadata support for some fixes. Unskip for local server only once we
    # upgrade to https://github.com/temporalio/sdk-python/issues/272.
    pytest.skip("Needs SDK metadata support")

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
    # TODO(cretz): Patches have issues on older servers since core needs patch
    # metadata support for some fixes. Unskip for local server only once we
    # upgrade to https://github.com/temporalio/sdk-python/issues/272.
    pytest.skip("Needs SDK metadata support")

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
            await workflow.wait_condition(lambda: self._done, timeout=0.01)
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
            TypedHandleWorkflow.run, id  # type: ignore[arg-type]
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
    with pytest.raises(WorkflowFailureError) as err:
        async with new_worker(
            client, CustomErrorWorkflow, activities=[custom_error_activity]
        ) as worker:
            handle = await client.start_workflow(
                CustomErrorWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
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
    bad_str = "bad-payload-str"

    def from_payloads(
        self, payloads: Sequence[Payload], type_hints: Optional[List] = None
    ) -> List[Any]:
        # Check if any payloads contain the bad data
        for payload in payloads:
            if ExceptionRaisingPayloadConverter.bad_str.encode() in payload.data:
                raise ApplicationError("Intentional converter failure")
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
                ExceptionRaisingPayloadConverter.bad_str,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert isinstance(err.value.cause, ApplicationError)
        assert "Intentional converter failure" in str(err.value.cause)


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


@workflow.defn
class SwallowGeneratorExitWorkflow:
    def __init__(self) -> None:
        self._signal_count = 0

    @workflow.run
    async def run(self) -> None:
        try:
            # Wait for signal count to reach 2
            await workflow.wait_condition(lambda: self._signal_count > 1)
        finally:
            # This finally, on eviction, is actually called because the above
            # await raises GeneratorExit. Then this will raise a
            # _NotInWorkflowEventLoopError swallowing that.
            await workflow.wait_condition(lambda: self._signal_count > 2)

    @workflow.signal
    async def signal(self) -> None:
        self._signal_count += 1

    @workflow.query
    async def signal_count(self) -> int:
        return self._signal_count


async def test_swallow_generator_exit(client: Client):
    if sys.version_info < (3, 8):
        pytest.skip("sys.unraisablehook not in 3.7")
    # This test simulates GeneratorExit and GC issues by forcing eviction on
    # each step
    async with new_worker(
        client, SwallowGeneratorExitWorkflow, max_cached_workflows=0
    ) as worker:
        # Put a hook to catch unraisable exceptions
        old_hook = sys.unraisablehook
        hook_calls: List[Any] = []
        sys.unraisablehook = hook_calls.append
        try:
            handle = await client.start_workflow(
                SwallowGeneratorExitWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

            async def signal_count() -> int:
                return await handle.query(SwallowGeneratorExitWorkflow.signal_count)

            # Confirm signal count as 0
            await assert_eq_eventually(0, signal_count)

            # Send signal and confirm it's at 1
            await handle.signal(SwallowGeneratorExitWorkflow.signal)
            await assert_eq_eventually(1, signal_count)

            await handle.signal(SwallowGeneratorExitWorkflow.signal)
            await assert_eq_eventually(2, signal_count)

            await handle.signal(SwallowGeneratorExitWorkflow.signal)
            await assert_eq_eventually(3, signal_count)

            await handle.result()
        finally:
            sys.unraisablehook = old_hook

        # Confirm no unraisable exceptions
        assert not hook_calls


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
