import asyncio
import dataclasses
import json
import logging
import logging.handlers
import queue
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Type,
    TypeVar,
    cast,
)

import pytest
from typing_extensions import Protocol, runtime_checkable

import temporalio.api.common.v1
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.converter import DataConverter, PayloadCodec
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    ChildWorkflowError,
    TimeoutError,
    WorkflowAlreadyStartedError,
)
from temporalio.worker import Worker
from temporalio.workflow_service import RPCError, RPCStatusCode


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


@workflow.defn
class MultiParamWorkflow:
    @workflow.run
    async def run(self, param1: int, param2: str) -> str:
        return f"param1: {param1}, param2: {param2}"


async def test_workflow_multi_param(client: Client):
    # This test is mostly just here to confirm MyPy type checks the multi-param
    # overload approach properly
    async with new_worker(client, MultiParamWorkflow) as worker:
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
        return json.loads(json.dumps(dataclasses.asdict(workflow.info()), default=str))


async def test_workflow_info(client: Client):
    async with new_worker(client, InfoWorkflow) as worker:
        workflow_id = f"workflow-{uuid.uuid4()}"
        info = await client.execute_workflow(
            InfoWorkflow.run, id=workflow_id, task_queue=worker.task_queue
        )
        assert info["attempt"] == 1
        assert info["cron_schedule"] is None
        assert info["execution_timeout"] is None
        assert info["namespace"] == client.namespace
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
    def signal_dynamic(self, name: str, *args: Any) -> None:
        self._last_event = f"signal_dynamic {name}: {args[0]}"

    @workflow.signal(name="Custom Name")
    def signal_custom(self, arg: str) -> None:
        self._last_event = f"signal_custom: {arg}"

    @workflow.query
    def last_event(self) -> str:
        return self._last_event or "<no event>"

    @workflow.query(dynamic=True)
    def query_dynamic(self, name: str, *args: Any) -> str:
        return f"query_dynamic {name}: {args[0]}"

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

        # Dynamic signals and queries
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
        def new_handler(name: str, *args: Any) -> None:
            self._last_event = f"signal dynamic {name}: {args[0]}"

        workflow.set_dynamic_signal_handler(new_handler)

    @workflow.signal
    def set_dynamic_query_handler(self) -> None:
        def new_handler(name: str, *args: Any) -> str:
            return f"query dynamic {name}: {args[0]}"

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
        ret["end"] = str(workflow.now())
        ret["event_loop_end"] = asyncio.get_running_loop().time()
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

        # Check the result
        assert datetime.fromisoformat(result["start"]) <= datetime.fromisoformat(
            result["end"]
        )
        assert isinstance(result["event_loop_start"], float)
        assert isinstance(result["event_loop_end"], float)
        assert result["event_loop_start"] <= result["event_loop_end"]


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


wait_cancel_complete: asyncio.Event


@activity.defn
async def wait_cancel() -> str:
    global wait_cancel_complete
    wait_cancel_complete.clear()
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
        wait_cancel_complete.set()


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
            handle = workflow.start_local_activity(
                wait_cancel,
                schedule_to_close_timeout=timedelta(seconds=5),
                cancellation_type=workflow.ActivityCancellationType[
                    params.cancellation_type
                ],
            )
        else:
            handle = workflow.start_activity(
                wait_cancel,
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
    global wait_cancel_complete
    wait_cancel_complete = asyncio.Event()
    # Need short task timeout to timeout LA task and longer assert timeout
    # so the task can timeout
    task_timeout = timedelta(seconds=1)
    assert_timeout = timedelta(seconds=10)

    async with new_worker(
        client, CancelActivityWorkflow, activities=[wait_cancel]
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
        await wait_cancel_complete.wait()
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
        await wait_cancel_complete.wait()
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
        assert not wait_cancel_complete.is_set()
        await handle.cancel()
        await wait_cancel_complete.wait()


@dataclass
class SimpleChildWorkflowParams:
    name: str
    child_id: str


@workflow.defn
class SimpleChildWorkflow:
    @workflow.run
    async def run(self, params: SimpleChildWorkflowParams) -> str:
        return await workflow.execute_child_workflow(
            HelloWorkflow.run, params.name, id=params.child_id
        )


async def test_workflow_simple_child(client: Client):
    async with new_worker(client, SimpleChildWorkflow, HelloWorkflow) as worker:
        result = await client.execute_workflow(
            SimpleChildWorkflow.run,
            SimpleChildWorkflowParams(
                name="Temporal", child_id=f"workflow-{uuid.uuid4()}"
            ),
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


@workflow.defn
class CancelChildWorkflow:
    @workflow.run
    async def run(self, child_id: str) -> None:
        handle = await workflow.start_child_workflow(LongSleepWorkflow.run, id=child_id)
        await asyncio.sleep(0.1)
        handle.cancel()
        await handle


async def test_workflow_cancel_child(client: Client):
    async with new_worker(client, CancelChildWorkflow, LongSleepWorkflow) as worker:
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                CancelChildWorkflow.run,
                f"child-workflow-{uuid.uuid4()}",
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert isinstance(err.value.cause, ChildWorkflowError)
        assert isinstance(err.value.cause.cause, CancelledError)


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
        handle = workflow.get_external_workflow_handle_for(
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
        await workflow.wait_condition(lambda: len(events) == 4, timeout=4)
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
    async def encode(
        self, payloads: Iterable[temporalio.api.common.v1.Payload]
    ) -> List[temporalio.api.common.v1.Payload]:
        wrapper = temporalio.api.common.v1.Payloads(payloads=payloads)
        return [
            temporalio.api.common.v1.Payload(
                metadata={"simple-codec": b"true"}, data=wrapper.SerializeToString()
            )
        ]

    async def decode(
        self, payloads: Iterable[temporalio.api.common.v1.Payload]
    ) -> List[temporalio.api.common.v1.Payload]:
        payloads = list(payloads)
        if len(payloads) != 1:
            raise RuntimeError("Expected only a single payload")
        elif payloads[0].metadata.get("simple-codec") != b"true":
            raise RuntimeError("Not encoded with this codec")
        wrapper = temporalio.api.common.v1.Payloads()
        wrapper.ParseFromString(payloads[0].data)
        return list(wrapper.payloads)


async def test_workflow_with_codec(client: Client):
    # Make client with this codec and run a couple of existing tests
    config = client.config()
    config["data_converter"] = DataConverter(payload_codec=SimpleCodec())
    client = Client(**config)
    await test_workflow_signal_and_query(client)
    await test_workflow_simple_activity(client)


@workflow.defn
class ContinueAsNewWorkflow:
    @workflow.run
    async def run(self, past_run_ids: List[str]) -> List[str]:
        if len(past_run_ids) == 5:
            return past_run_ids
        info = workflow.info()
        if info.continued_run_id:
            past_run_ids.append(info.continued_run_id)
        workflow.continue_as_new(past_run_ids)


async def test_workflow_continue_as_new(client: Client):
    async with new_worker(client, ContinueAsNewWorkflow) as worker:
        handle = await client.start_workflow(
            ContinueAsNewWorkflow.run,
            cast(List[str], []),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await handle.result()
        assert len(result) == 5
        assert result[0] == handle.first_execution_run_id


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


async def test_workflow_logging(client: Client):
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
        # Log two signals and kill worker before completing
        async with new_worker(client, LoggingWorkflow) as worker:
            handle = await client.start_workflow(
                LoggingWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            # Send a couple signals
            async def last_event() -> str:
                return await handle.query(LoggingWorkflow.last_signal)

            await handle.signal(LoggingWorkflow.my_signal, "signal 1")
            await handle.signal(LoggingWorkflow.my_signal, "signal 2")
            assert "signal 2" == await handle.query(LoggingWorkflow.last_signal)

        # Confirm two logs happened
        assert find_log("Signal: signal 1 ({'workflow_id':")
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
            client, LoggingWorkflow, task_queue=worker.task_queue
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
                schedule_to_close_timeout=timedelta(seconds=2),
            )
            param.assert_expected()
            param = await workflow.execute_local_activity(
                data_class_typed_activity,
                param,
                schedule_to_close_timeout=timedelta(seconds=2),
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


async def test_workflow_dataclass_typed(client: Client):
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


async def test_workflow_already_started(client: Client):
    async with new_worker(client, LongSleepWorkflow) as worker:
        id = f"workflow-{uuid.uuid4()}"
        # Try to start it twice
        with pytest.raises(RPCError) as err:
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
        assert err.value.status == RPCStatusCode.ALREADY_EXISTS


@workflow.defn
class ChildAlreadyStartedWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Try to start it twice
        id = f"{workflow.info().workflow_id}_child"
        await workflow.start_child_workflow(LongSleepWorkflow.run, id=id)
        # TODO(cretz): If we just let this throw, it is a WFT failure and not a
        # workflow failure since WorkflowAlreadyStartedError isn't a "failure
        # error". Is this ok?
        try:
            await workflow.start_child_workflow(LongSleepWorkflow.run, id=id)
        except WorkflowAlreadyStartedError:
            raise ApplicationError("Already started")


async def test_workflow_child_already_started(client: Client):
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


# TODO:
# * Use typed dicts for activity, local activity, and child workflow configs
# * Local activity invalid options
# * Local activity backoff (and cancel during backoff)
# * Cancelling things whose commands haven't been sent
# * Starting something on an already-cancelled cancelled task
# * Cancel unstarted child
# * Cancel unstarted child from a signal in the same WFT that "child started" may be in later
# * Explicit create_task and create_future cancelling
# * Cancel only after N attempts (i.e. showing cancel is an event not a state)
# * Signal handler errors (treated as workflow errors)
# * Exception details with codec
# * Custom workflow runner that also confirms WorkflowInstanceDetails can be pickled
# * Deadlock detection
# * Non-query commands after workflow complete
# * Query after workflow (succeed and fail)
# * More workflow info data


def new_worker(
    client: Client,
    *workflows: Type,
    activities: Iterable[Callable] = [],
    task_queue: Optional[str] = None,
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue or str(uuid.uuid4()),
        workflows=workflows,
        activities=activities,
    )


T = TypeVar("T")


async def assert_eq_eventually(
    expected: T,
    fn: Callable[[], Awaitable[T]],
    *,
    timeout: timedelta = timedelta(seconds=3),
    interval: timedelta = timedelta(milliseconds=200),
) -> None:
    start_sec = time.monotonic()
    last_value = None
    while timedelta(seconds=time.monotonic() - start_sec) < timeout:
        last_value = await fn()
        if expected == last_value:
            return
        await asyncio.sleep(interval.total_seconds())
    assert (
        expected == last_value
    ), "timed out waiting for equal, asserted against last value"
