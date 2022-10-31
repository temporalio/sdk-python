import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import AsyncIterator

import pytest

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from tests.helpers import assert_eq_eventually


@activity.defn
async def say_hello(name: str) -> str:
    return f"Hello, {name}!"


@dataclass
class SayHelloParams:
    name: str
    should_wait: bool = False
    should_error: bool = False
    should_cause_nondeterminism: bool = False


@workflow.defn
class SayHelloWorkflow:
    def __init__(self) -> None:
        self._waiting = False
        self._finish = False

    @workflow.run
    async def run(self, params: SayHelloParams) -> str:
        result = await workflow.execute_activity(
            say_hello, params.name, schedule_to_close_timeout=timedelta(seconds=60)
        )

        # Wait if requested
        if params.should_wait:
            self._waiting = True
            await workflow.wait_condition(lambda: self._finish)
            self._waiting = False

        # Raise if requested
        if params.should_error:
            raise ApplicationError("Intentional error")

        # Cause non-determinism if requested
        if params.should_cause_nondeterminism:
            if workflow.unsafe.is_replaying():
                await asyncio.sleep(0.1)

        return result

    @workflow.signal
    def finish(self) -> None:
        self._finish = True

    @workflow.query
    def waiting(self) -> bool:
        return self._waiting


async def test_replayer_workflow_complete(client: Client) -> None:
    # Run workflow to completion
    async with new_say_hello_worker(client) as worker:
        handle = await client.start_workflow(
            SayHelloWorkflow.run,
            SayHelloParams(name="Temporal"),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert "Hello, Temporal!" == await handle.result()

    # Collect history and replay it
    await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(
        await handle.fetch_history()
    )


async def test_replayer_workflow_complete_json() -> None:
    with Path(__file__).with_name("test_replayer_complete_history.json").open("r") as f:
        history_json = f.read()
    await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(
        WorkflowHistory.from_json("fake", history_json)
    )


async def test_replayer_workflow_incomplete(client: Client) -> None:
    # Run workflow to wait point
    async with new_say_hello_worker(client) as worker:
        handle = await client.start_workflow(
            SayHelloWorkflow.run,
            SayHelloParams(name="Temporal", should_wait=True),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Wait until it's waiting
        async def waiting() -> bool:
            return await handle.query(SayHelloWorkflow.waiting)

        await assert_eq_eventually(True, waiting)

    # Collect history and replay it
    await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(
        await handle.fetch_history()
    )


async def test_replayer_workflow_failed(client: Client) -> None:
    # Run workflow to failure completion
    async with new_say_hello_worker(client) as worker:
        handle = await client.start_workflow(
            SayHelloWorkflow.run,
            SayHelloParams(name="Temporal", should_error=True),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()
        assert err.value.cause.message == "Intentional error"

    # Collect history and replay it
    await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(
        await handle.fetch_history()
    )


async def test_replayer_workflow_nondeterministic(client: Client) -> None:
    # Run workflow to completion
    async with new_say_hello_worker(client) as worker:
        handle = await client.start_workflow(
            SayHelloWorkflow.run,
            SayHelloParams(name="Temporal", should_cause_nondeterminism=True),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()

    # Collect history and replay it expecting error
    with pytest.raises(workflow.NondeterminismError):
        await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(
            await handle.fetch_history()
        )


async def test_replayer_workflow_nondeterministic_json() -> None:
    with Path(__file__).with_name("test_replayer_nondeterministic_history.json").open(
        "r"
    ) as f:
        history_json = f.read()
    with pytest.raises(workflow.NondeterminismError):
        await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(
            WorkflowHistory.from_json("fake", history_json)
        )


async def test_replayer_multiple_histories_fail_fast() -> None:
    with Path(__file__).with_name("test_replayer_complete_history.json").open("r") as f:
        history_json = f.read()
    with Path(__file__).with_name("test_replayer_nondeterministic_history.json").open(
        "r"
    ) as f:
        history_json_bad = f.read()

    callcount = 0

    async def histories():
        nonlocal callcount
        callcount += 1
        yield WorkflowHistory.from_json("fake_bad", history_json_bad)
        # Must sleep so this coroutine can be interrupted by early exit
        await asyncio.sleep(1)
        callcount += 1
        yield WorkflowHistory.from_json("fake", history_json)

    with pytest.raises(workflow.NondeterminismError):
        await Replayer(workflows=[SayHelloWorkflow]).replay_workflows(histories())

    # We should only have replayed the fist history since we fail fast
    assert callcount == 1


async def test_replayer_multiple_histories_fail_slow() -> None:
    with Path(__file__).with_name("test_replayer_complete_history.json").open("r") as f:
        history_json = f.read()
    with Path(__file__).with_name("test_replayer_nondeterministic_history.json").open(
        "r"
    ) as f:
        history_json_bad = f.read()

    callcount = 0
    bad_hist = WorkflowHistory.from_json("fake_bad", history_json_bad)
    bad_hist_run_id = bad_hist.events[
        0
    ].workflow_execution_started_event_attributes.original_execution_run_id

    async def histories():
        nonlocal callcount
        callcount += 1
        yield bad_hist
        callcount += 1
        yield WorkflowHistory.from_json("fake", history_json)
        callcount += 1
        h3 = WorkflowHistory.from_json("fake", history_json)
        # Need to give a new run id to ensure playback continues
        h3.events[
            0
        ].workflow_execution_started_event_attributes.original_execution_run_id = "r3"
        h3.events[
            0
        ].workflow_execution_started_event_attributes.first_execution_run_id = "r3"
        yield h3
        callcount += 1

    results = await Replayer(workflows=[SayHelloWorkflow]).replay_workflows(
        histories(), raise_on_replay_failure=False
    )

    assert callcount == 4
    assert results.replay_failures
    assert results.replay_failures[bad_hist_run_id] is not None


@workflow.defn
class SayHelloWorkflowDifferent:
    @workflow.run
    async def run(self) -> None:
        pass


async def test_replayer_workflow_not_registered(client: Client) -> None:
    # Run workflow to completion
    async with new_say_hello_worker(client) as worker:
        handle = await client.start_workflow(
            SayHelloWorkflow.run,
            SayHelloParams(name="Temporal"),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()

    # Collect history and replay it expecting error
    with pytest.raises(RuntimeError) as err:
        await Replayer(workflows=[SayHelloWorkflowDifferent]).replay_workflow(
            await handle.fetch_history()
        )
    assert "SayHelloWorkflow is not registered" in str(err.value)


async def test_replayer_multiple_from_client(
    client: Client, env: WorkflowEnvironment
) -> None:
    if env.supports_time_skipping:
        pytest.skip("Java test server doesn't support newer workflow listing")

    # Run 5 say-hello's, with second and 4th having non-det errors. Use the same
    # workflow ID for all of them so that we can query.
    workflow_id_suffix = f"-{uuid.uuid4()}"
    async with new_say_hello_worker(client) as worker:
        workflow_ids = []
        for i in range(5):
            workflow_id = f"workflow-{i}{workflow_id_suffix}"
            workflow_ids.append(workflow_id)
            await client.execute_workflow(
                SayHelloWorkflow.run,
                SayHelloParams(
                    name="Temporal", should_cause_nondeterminism=i == 1 or i == 3
                ),
                id=workflow_id,
                task_queue=worker.task_queue,
            )

    # Lazily list then fetch histories the workflows, manually filtering for
    # the ones we expect
    async def hist_iter() -> AsyncIterator[WorkflowHistory]:
        async for exec in await client.list_workflows(
            "WorkflowType = 'SayHelloWorkflow'"
        ):
            if exec.id.endswith(workflow_id_suffix):
                yield await client.get_workflow_handle(exec.id).fetch_history()

    async with Replayer(workflows=[SayHelloWorkflow]).workflow_replay_iterator(
        hist_iter()
    ) as result_iter:
        results = [r async for r in result_iter]

    # Sort the results and check against expected
    results = sorted(results, key=lambda r: r.history.workflow_id)
    assert len(results) == 5
    for i, workflow_id in enumerate(workflow_ids):
        assert results[i].history.workflow_id == workflow_id
        if i == 1 or i == 3:
            assert isinstance(results[i].replay_failure, workflow.NondeterminismError)
        else:
            assert not results[i].replay_failure


def new_say_hello_worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[SayHelloWorkflow],
        activities=[say_hello],
    )
