import asyncio
import dataclasses
import json
import logging
import logging.handlers
import queue
import time
import uuid
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

import temporalio.api.common.v1
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.converter import DataConverter, PayloadCodec
from temporalio.exceptions import ActivityError, CancelledError, ChildWorkflowError
from temporalio.worker import Worker


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

        async def last_event() -> str:
            return await handle.query(SignalAndQueryWorkflow.last_event)

        # Simple signals and queries
        await handle.signal(SignalAndQueryWorkflow.signal1, "some arg")
        await assert_eq_eventually("signal1: some arg", last_event)

        # Dynamic signals and queries
        await handle.signal("signal2", "dyn arg")
        await assert_eq_eventually("signal_dynamic signal2: dyn arg", last_event)
        assert "query_dynamic query2: dyn arg" == await handle.query(
            "query2", "dyn arg"
        )

        # Custom named signals and queries
        await handle.signal("Custom Name", "custom arg1")
        await assert_eq_eventually("signal_custom: custom arg1", last_event)
        await handle.signal(SignalAndQueryWorkflow.signal_custom, "custom arg2")
        await assert_eq_eventually("signal_custom: custom arg2", last_event)
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

        async def last_event() -> str:
            return await handle.query(SignalAndQueryHandlersWorkflow.last_event)

        # Confirm signals buffered when not found
        await handle.signal("unknown_signal1", "val1")
        await handle.signal(
            SignalAndQueryHandlersWorkflow.set_signal_handler, "unknown_signal1"
        )
        await assert_eq_eventually("signal unknown_signal1: val1", last_event)

        # Normal signal handling
        await handle.signal("unknown_signal1", "val2")
        await assert_eq_eventually("signal unknown_signal1: val2", last_event)

        # Dynamic signal handling buffered and new
        await handle.signal("unknown_signal2", "val3")
        await handle.signal(SignalAndQueryHandlersWorkflow.set_dynamic_signal_handler)
        await assert_eq_eventually("signal dynamic unknown_signal2: val3", last_event)
        await handle.signal("unknown_signal3", "val4")
        await assert_eq_eventually("signal dynamic unknown_signal3: val4", last_event)

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


@activity.defn
async def wait_cancel() -> str:
    try:
        while True:
            await asyncio.sleep(0.3)
            activity.heartbeat()
    except asyncio.CancelledError:
        return "Got cancelled error, cancelled? " + str(activity.is_cancelled())


@workflow.defn
class CancelActivityWorkflow:
    @workflow.run
    async def run(self, wait_for_cancel: bool) -> str:
        cancellation_type = workflow.ActivityCancellationType.TRY_CANCEL
        if wait_for_cancel:
            cancellation_type = (
                workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
            )
        handle = workflow.start_activity(
            wait_cancel,
            schedule_to_close_timeout=timedelta(seconds=5),
            heartbeat_timeout=timedelta(seconds=1),
            cancellation_type=cancellation_type,
        )
        await asyncio.sleep(0.01)
        handle.cancel()
        return await handle


async def test_workflow_cancel_activity(client: Client):
    async with new_worker(
        client, CancelActivityWorkflow, activities=[wait_cancel]
    ) as worker:
        # Do not wait for cancel
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                CancelActivityWorkflow.run,
                False,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
        assert isinstance(err.value.cause, ActivityError)
        assert isinstance(err.value.cause.cause, CancelledError)

        # Wait for cancel
        result = await client.execute_workflow(
            CancelActivityWorkflow.run,
            True,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert "Got cancelled error, cancelled? True" == result


@activity.defn
async def wait_local_cancel() -> str:
    try:
        await asyncio.sleep(1000)
        raise RuntimeError("Should not get here")
    except asyncio.CancelledError:
        return "Got cancelled error, cancelled? " + str(activity.is_cancelled())


@workflow.defn
class CancelLocalActivityWorkflow:
    @workflow.run
    async def run(self, wait_for_cancel: bool) -> str:
        cancellation_type = workflow.ActivityCancellationType.TRY_CANCEL
        if wait_for_cancel:
            cancellation_type = (
                workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
            )
        handle = workflow.start_local_activity(
            wait_local_cancel,
            schedule_to_close_timeout=timedelta(seconds=5),
            cancellation_type=cancellation_type,
        )
        await asyncio.sleep(0.01)
        handle.cancel()
        return await handle


# # @pytest.mark.skip(reason="Make test to timeout WFT better")
async def test_workflow_cancel_local_activity(client: Client):
    async with new_worker(
        client, CancelLocalActivityWorkflow, activities=[wait_local_cancel]
    ) as worker:
        # Do not wait for cancel
        with pytest.raises(WorkflowFailureError) as err:
            await client.execute_workflow(
                CancelLocalActivityWorkflow.run,
                False,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                # This has to be low to timeout the LA task
                task_timeout=timedelta(seconds=1),
            )
        # TODO(cretz): Fix when https://github.com/temporalio/sdk-core/issues/323 is fixed
        # assert isinstance(err.value.cause, ActivityError)
        assert isinstance(err.value.cause, CancelledError)

        # Wait for cancel
        result = await client.execute_workflow(
            CancelLocalActivityWorkflow.run,
            True,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            # This has to be low to timeout the LA task
            task_timeout=timedelta(seconds=1),
        )
        assert "Got cancelled error, cancelled? True" == result


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
            await assert_eq_eventually("signal 2", last_event)

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


# TODO:
# * Activity timeout behavior
# * Data class params and return types
# * Separate protocol and impl
# * Separate ABC and impl
# * Workflow execution already started error (from client _and_ child workflow)
# * Use typed dicts for activity, local activity, and child workflow configs
# * Local activity invalid options
# * Local activity backoff
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
