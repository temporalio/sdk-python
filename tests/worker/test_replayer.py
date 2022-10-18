import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from google.protobuf import json_format

from temporalio import activity, workflow
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import (
    CancelExternalWorkflowExecutionFailedCause,
    ContinueAsNewInitiator,
    EventType,
    ParentClosePolicy,
    RetryState,
    SignalExternalWorkflowExecutionFailedCause,
    StartChildWorkflowExecutionFailedCause,
    TaskQueueKind,
    TimeoutType,
    WorkflowIdReusePolicy,
    WorkflowTaskFailedCause,
)
from temporalio.api.history.v1 import History
from temporalio.api.workflowservice.v1 import GetWorkflowExecutionHistoryRequest
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker, WorkflowHistory
from temporalio.worker.replayer import _history_from_json
from tests.worker.test_workflow import assert_eq_eventually


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
    history = await get_history(client, handle.id)
    await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(history)


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
    history = await get_history(client, handle.id)
    await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(history)


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
    history = await get_history(client, handle.id)
    await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(history)


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
    history = await get_history(client, handle.id)
    with pytest.raises(workflow.NondeterminismError) as err:
        await Replayer(workflows=[SayHelloWorkflow]).replay_workflow(history)


async def test_replayer_workflow_nondeterministic_json() -> None:
    with Path(__file__).with_name("test_replayer_nondeterministic_history.json").open(
        "r"
    ) as f:
        history_json = f.read()
    with pytest.raises(workflow.NondeterminismError) as err:
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

    results = await Replayer(
        workflows=[SayHelloWorkflow], fail_fast=False
    ).replay_workflows(histories())

    assert callcount == 4
    assert results.had_any_failure()
    assert results.failure_details[bad_hist_run_id] is not None


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
    history = await get_history(client, handle.id)
    with pytest.raises(RuntimeError) as err:
        await Replayer(workflows=[SayHelloWorkflowDifferent]).replay_workflow(history)
    assert "SayHelloWorkflow is not registered" in str(err.value)


def new_say_hello_worker(client: Client) -> Worker:
    return Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[SayHelloWorkflow],
        activities=[say_hello],
    )


async def get_history(client: Client, workflow_id: str) -> WorkflowHistory:
    history = History()
    req = GetWorkflowExecutionHistoryRequest(
        namespace=client.namespace, execution=WorkflowExecution(workflow_id=workflow_id)
    )
    while True:
        resp = await client.workflow_service.get_workflow_execution_history(req)
        history.events.extend(resp.history.events)
        if not resp.next_page_token:
            return WorkflowHistory(workflow_id, history.events)
        req.next_page_token = resp.next_page_token


def test_history_from_json():
    # Take proto, make JSON, convert to dict, alter some enums, confirm that it
    # alters the enums back and matches original history

    # Make history with some enums, one one each event
    history = History()
    history.events.add().request_cancel_external_workflow_execution_failed_event_attributes.cause = (
        CancelExternalWorkflowExecutionFailedCause.CANCEL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_EXTERNAL_WORKFLOW_EXECUTION_NOT_FOUND
    )
    history.events.add().workflow_execution_started_event_attributes.initiator = (
        ContinueAsNewInitiator.CONTINUE_AS_NEW_INITIATOR_CRON_SCHEDULE
    )
    history.events.add().event_type = (
        EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED
    )
    history.events.add().start_child_workflow_execution_initiated_event_attributes.parent_close_policy = (
        ParentClosePolicy.PARENT_CLOSE_POLICY_ABANDON
    )
    history.events.add().workflow_execution_failed_event_attributes.retry_state = (
        RetryState.RETRY_STATE_CANCEL_REQUESTED
    )
    history.events.add().signal_external_workflow_execution_failed_event_attributes.cause = (
        SignalExternalWorkflowExecutionFailedCause.SIGNAL_EXTERNAL_WORKFLOW_EXECUTION_FAILED_CAUSE_EXTERNAL_WORKFLOW_EXECUTION_NOT_FOUND
    )
    history.events.add().start_child_workflow_execution_failed_event_attributes.cause = (
        StartChildWorkflowExecutionFailedCause.START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_NAMESPACE_NOT_FOUND
    )
    history.events.add().workflow_execution_started_event_attributes.task_queue.kind = (
        TaskQueueKind.TASK_QUEUE_KIND_NORMAL
    )
    history.events.add().workflow_task_timed_out_event_attributes.timeout_type = (
        TimeoutType.TIMEOUT_TYPE_HEARTBEAT
    )
    history.events.add().start_child_workflow_execution_initiated_event_attributes.workflow_id_reuse_policy = (
        WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE
    )
    history.events.add().workflow_task_failed_event_attributes.cause = (
        WorkflowTaskFailedCause.WORKFLOW_TASK_FAILED_CAUSE_BAD_BINARY
    )
    history.events.add().workflow_execution_started_event_attributes.continued_failure.timeout_failure_info.timeout_type = (
        TimeoutType.TIMEOUT_TYPE_SCHEDULE_TO_CLOSE
    )
    history.events.add().activity_task_started_event_attributes.last_failure.activity_failure_info.retry_state = (
        RetryState.RETRY_STATE_IN_PROGRESS
    )
    history.events.add().workflow_execution_failed_event_attributes.failure.cause.child_workflow_execution_failure_info.retry_state = (
        RetryState.RETRY_STATE_INTERNAL_SERVER_ERROR
    )

    # Convert to JSON dict and alter enums to pascal versions
    bad_history_dict = json_format.MessageToDict(history)
    e = bad_history_dict["events"]
    e[0]["requestCancelExternalWorkflowExecutionFailedEventAttributes"][
        "cause"
    ] = "ExternalWorkflowExecutionNotFound"
    e[1]["workflowExecutionStartedEventAttributes"]["initiator"] = "CronSchedule"
    e[2]["eventType"] = "ActivityTaskCancelRequested"
    e[3]["startChildWorkflowExecutionInitiatedEventAttributes"][
        "parentClosePolicy"
    ] = "Abandon"
    e[4]["workflowExecutionFailedEventAttributes"]["retryState"] = "CancelRequested"
    e[5]["signalExternalWorkflowExecutionFailedEventAttributes"][
        "cause"
    ] = "ExternalWorkflowExecutionNotFound"
    e[6]["startChildWorkflowExecutionFailedEventAttributes"][
        "cause"
    ] = "NamespaceNotFound"
    e[7]["workflowExecutionStartedEventAttributes"]["taskQueue"]["kind"] = "Normal"
    e[8]["workflowTaskTimedOutEventAttributes"]["timeoutType"] = "Heartbeat"
    e[9]["startChildWorkflowExecutionInitiatedEventAttributes"][
        "workflowIdReusePolicy"
    ] = "AllowDuplicate"
    e[10]["workflowTaskFailedEventAttributes"]["cause"] = "BadBinary"
    e[11]["workflowExecutionStartedEventAttributes"]["continuedFailure"][
        "timeoutFailureInfo"
    ]["timeoutType"] = "ScheduleToClose"
    e[12]["activityTaskStartedEventAttributes"]["lastFailure"]["activityFailureInfo"][
        "retryState"
    ] = "InProgress"
    e[13]["workflowExecutionFailedEventAttributes"]["failure"]["cause"][
        "childWorkflowExecutionFailureInfo"
    ]["retryState"] = "InternalServerError"

    # Apply fixes
    history_from_dict = _history_from_json(bad_history_dict)
    history_from_json = _history_from_json(json.dumps(bad_history_dict))

    # Check
    assert json_format.MessageToDict(history) == json_format.MessageToDict(
        history_from_dict
    )
    assert json_format.MessageToDict(history) == json_format.MessageToDict(
        history_from_json
    )

    # Confirm double-encode does not cause issues
    assert json_format.MessageToDict(history) == json_format.MessageToDict(
        _history_from_json(json_format.MessageToDict(history_from_dict))
    )
