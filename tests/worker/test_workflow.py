import asyncio
import time
import uuid
from datetime import timedelta
from typing import Awaitable, Callable, Type

from temporalio import workflow
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
            HelloWorkflow.run, "Temporal", id="workflow1", task_queue=worker.task_queue
        )
        assert result == "Hello, Temporal!"


@workflow.defn
class MultiFeatureWorkflow:
    @workflow.run
    async def run(self) -> None:
        # Wait forever
        await asyncio.Future()

    @workflow.signal
    def signal1(self, arg: str) -> None:
        self._last_event = f"signal1: {arg}"

    @workflow.query
    def last_event(self) -> str:
        return self._last_event or "<no event>"


async def test_workflow_multi_feature(client: Client):
    async with new_worker(client, MultiFeatureWorkflow) as worker:
        handle = await client.start_workflow(
            MultiFeatureWorkflow.run,
            id="workflow1",
            task_queue=worker.task_queue,
        )

        async def last_event() -> str:
            return await handle.query(MultiFeatureWorkflow.last_event)

        # Send signal and query that it was received
        await handle.signal(MultiFeatureWorkflow.signal1, "some arg")
        await assert_eq_eventually("signal1: some arg", last_event)

        # TODO(cretz): More features


def new_worker(client: Client, *workflows: Type) -> Worker:
    return Worker(client, task_queue=str(uuid.uuid4()), workflows=workflows)


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
