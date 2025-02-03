import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Type

import pytest

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    Replayer,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)
from tests.helpers import assert_eq_eventually
from tests.worker.test_workflow import (
    ActivityAndSignalsWhileWorkflowDown,
    SignalsActivitiesTimersUpdatesTracingWorkflow,
)


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


@pytest.mark.skipif(sys.version_info < (3, 12), reason="Skipping for < 3.12")
async def test_replayer_workflow_complete(client: Client) -> None:
    # This test skips for versions < 3.12 because this is flaky due to CPython reimport issue:
    # https://github.com/python/cpython/issues/91351

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


@pytest.mark.skipif(sys.version_info < (3, 12), reason="Skipping for < 3.12")
async def test_replayer_workflow_complete_json() -> None:
    # See `test_replayer_workflow_complete` for full skip description.

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
        assert isinstance(err.value.cause, ApplicationError)
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
    with (
        Path(__file__)
        .with_name("test_replayer_nondeterministic_history.json")
        .open("r") as f
    ):
        history_json = f.read()
    with pytest.raises(workflow.NondeterminismError):
        await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(
            WorkflowHistory.from_json("fake", history_json)
        )


async def test_replayer_multiple_histories_fail_fast() -> None:
    with Path(__file__).with_name("test_replayer_complete_history.json").open("r") as f:
        history_json = f.read()
    with (
        Path(__file__)
        .with_name("test_replayer_nondeterministic_history.json")
        .open("r") as f
    ):
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
    with (
        Path(__file__)
        .with_name("test_replayer_nondeterministic_history.json")
        .open("r") as f
    ):
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

    # Run 5 say-hello's, with 2nd and 4th having non-det errors. Reuse the same
    # workflow ID so we can query it using standard visibility.
    workflow_id = f"workflow-{uuid.uuid4()}"
    async with new_say_hello_worker(client) as worker:
        expected_runs_and_non_det: Dict[str, bool] = {}
        for i in range(5):
            should_cause_nondeterminism = i == 1 or i == 3
            handle = await client.start_workflow(
                SayHelloWorkflow.run,
                SayHelloParams(
                    name="Temporal",
                    should_cause_nondeterminism=should_cause_nondeterminism,
                ),
                id=workflow_id,
                task_queue=worker.task_queue,
            )
            assert handle.result_run_id
            expected_runs_and_non_det[handle.result_run_id] = (
                should_cause_nondeterminism
            )
            await handle.result()

    # Run replayer with list iterator mapped to histories and collect results
    async with Replayer(workflows=[SayHelloWorkflow]).workflow_replay_iterator(
        client.list_workflows(f"WorkflowId = '{workflow_id}'").map_histories()
    ) as result_iter:
        actual_runs_and_non_det = {
            r.history.run_id: isinstance(r.replay_failure, workflow.NondeterminismError)
            async for r in result_iter
        }

    assert expected_runs_and_non_det == actual_runs_and_non_det


def new_say_hello_worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[SayHelloWorkflow],
        activities=[say_hello],
    )


@workflow.defn
class UpdateCompletionAfterWorkflowReturn:
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


async def test_replayer_command_reordering_backward_compatibility() -> None:
    """
    The UpdateCompletionAfterWorkflowReturn workflow above features an update handler that returns
    after the main workflow coroutine has exited. It will (if an update is sent in the first WFT)
    generate a raw command sequence (before sending to core) of

    [UpdateAccepted, CompleteWorkflowExecution, UpdateCompleted].

    Prior to https://github.com/temporalio/sdk-python/pull/569, Python truncated this command
    sequence to

    [UpdateAccepted, CompleteWorkflowExecution].

    With #569, Python performs no truncation, and Core changes it to

    [UpdateAccepted, UpdateCompleted, CompleteWorkflowExecution].

    This test takes a history generated using pre-#569 SDK code, and replays it. This succeeds.
    The history is

    1 WorkflowExecutionStarted
    2 WorkflowTaskScheduled
    3 WorkflowTaskStarted
    4 WorkflowTaskCompleted
    5 WorkflowExecutionUpdateAccepted
    6 WorkflowExecutionCompleted

    Note that the history lacks a WorkflowExecutionUpdateCompleted event.

    If Core's logic (which involves a flag) incorrectly allowed this history to be replayed
    using Core's post-#569 implementation, then a non-determinism error would result. Specifically,
    Core would, at some point during replay, do the following:

    Receive [UpdateAccepted, CompleteWorkflowExecution, UpdateCompleted] from lang,
    change that to [UpdateAccepted, UpdateCompleted, CompleteWorkflowExecution]
    and create an UpdateMachine instance (the WorkflowTaskMachine instance already exists).
    Then continue to consume history events.

    Event 5 WorkflowExecutionUpdateAccepted would apply to the UpdateMachine associated with
    the UpdateAccepted command, but event 6 WorkflowExecutionCompleted would not, since
    core is expecting an event that can be applied to the UpdateMachine corresponding to
    UpdateCompleted. If we modify core to incorrectly apply its new logic then we do see that:

    [TMPRL1100] Nondeterminism error: Update machine does not handle this event: HistoryEvent(id: 6, WorkflowExecutionCompleted)

    The test passes because core in fact (because the history lacks the flag) uses its old logic
    and changes the command sequence from [UpdateAccepted, CompleteWorkflowExecution, UpdateCompleted]
    to [UpdateAccepted, CompleteWorkflowExecution], and events 5 and 6 can be applied to the
    corresponding state machines.
    """
    with (
        Path(__file__)
        .with_name("test_replayer_command_reordering_backward_compatibility.json")
        .open() as f
    ):
        history = f.read()
    await Replayer(workflows=[UpdateCompletionAfterWorkflowReturn]).replay_workflow(
        WorkflowHistory.from_json("fake", history)
    )


test_replayer_workflow_res = None


class WorkerWorkflowResultInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        return WorkflowResultInterceptor


class WorkflowResultInterceptor(WorkflowInboundInterceptor):
    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        global test_replayer_workflow_res
        res = await super().execute_workflow(input)
        test_replayer_workflow_res = res
        return res


async def test_replayer_async_ordering() -> None:
    """
    This test verifies that the order that asyncio tasks/coroutines are woken up matches the
    order they were before changes to apply all jobs and then run the event loop, where previously
    the event loop was ran after each "batch" of jobs.
    """
    histories_and_expecteds = [
        (
            "test_replayer_event_tracing.json",
            [
                "sig-before-sync",
                "sig-before-1",
                "sig-before-2",
                "timer-sync",
                "act-sync",
                "act-1",
                "act-2",
                "sig-1-sync",
                "sig-1-1",
                "sig-1-2",
                "update-1-sync",
                "update-1-1",
                "update-1-2",
                "timer-1",
                "timer-2",
            ],
        ),
        (
            "test_replayer_event_tracing_double_sig_at_start.json",
            [
                "sig-before-sync",
                "sig-before-1",
                "sig-1-sync",
                "sig-1-1",
                "sig-before-2",
                "sig-1-2",
                "timer-sync",
                "act-sync",
                "update-1-sync",
                "update-1-1",
                "update-1-2",
                "act-1",
                "act-2",
                "timer-1",
                "timer-2",
            ],
        ),
    ]
    for history, expected in histories_and_expecteds:
        with Path(__file__).with_name(history).open() as f:
            history = f.read()
        await Replayer(
            workflows=[SignalsActivitiesTimersUpdatesTracingWorkflow],
            interceptors=[WorkerWorkflowResultInterceptor()],
        ).replay_workflow(WorkflowHistory.from_json("fake", history))
        assert test_replayer_workflow_res == expected


async def test_replayer_alternate_async_ordering() -> None:
    with (
        Path(__file__)
        .with_name("test_replayer_event_tracing_alternate.json")
        .open() as f
    ):
        history = f.read()
    await Replayer(
        workflows=[ActivityAndSignalsWhileWorkflowDown],
        interceptors=[WorkerWorkflowResultInterceptor()],
    ).replay_workflow(WorkflowHistory.from_json("fake", history))
    assert test_replayer_workflow_res == [
        "act-start",
        "sig-1",
        "sig-2",
        "counter-2",
        "act-done",
    ]
