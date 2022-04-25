import asyncio
import dataclasses
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Iterable, Type

from temporalio import activity, workflow
from temporalio.client import Client
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


def new_worker(
    client: Client, *workflows: Type, activities: Iterable[Callable] = []
) -> Worker:
    return Worker(
        client, task_queue=str(uuid.uuid4()), workflows=workflows, activities=activities
    )


async def assert_eq_eventually(
    expected: str,
    fn: Callable[[], Awaitable[str]],
    *,
    timeout: timedelta = timedelta(seconds=3),
    interval: timedelta = timedelta(milliseconds=200),
) -> None:
    start_sec = time.monotonic()
    last_value = "<no value>"
    while timedelta(seconds=time.monotonic() - start_sec) < timeout:
        last_value = await fn()
        if expected == last_value:
            return
        await asyncio.sleep(interval.total_seconds())
    assert (
        expected == last_value
    ), "timed out waiting for equal, asserted against last value"
