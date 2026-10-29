"""Opt-in SDK/server tests; see STICKY_ACTIVITY_INTEGRATION.md."""

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

import temporalio.activity as activity
import temporalio.api.enums.v1 as enums
import temporalio.client as client_api
import temporalio.common as common
import temporalio.exceptions as exceptions
import temporalio.worker as worker
import temporalio.workflow as workflow
from tests.helpers import (
    assert_eventually,
    assert_task_queue_poller_eventually,
    assert_workflow_and_activities_on_worker,
    events_of_type,
    new_worker,
)

IN_WORKFLOW_SESSION = common.ActivitySessionOptions(
    placement=common.UseWorkflowSession()
)

pytestmark = pytest.mark.skipif(
    os.getenv("TEMPORAL_TEST_STICKY_ACTIVITIES") != "1",
    reason="requires the sticky-activity server branch and namespace gate",
)


@pytest.fixture
async def other_client(client: client_api.Client, env_type: str) -> client_api.Client:
    if env_type in ("local", "time-skipping"):
        pytest.fail(
            "Pass --workflow-environment host:port or envconfig for the custom server"
        )
    # Reconnect rather than share the client-wide worker registry.
    config: dict[str, Any] = {
        **vars(client.service_client.config),
        **client.config(),
    }
    config.pop("service_client")
    config.update(identity=f"other-worker-{uuid.uuid4()}", lazy=False)
    return await client_api.Client.connect(**config)


@workflow.defn
class SessionRetryWorkflow:
    @workflow.run
    async def run(self) -> None:
        with workflow.activity_session(IN_WORKFLOW_SESSION):
            activities = [
                workflow.start_activity(
                    "retry_session_activity",
                    activity_id=f"retry-{index}",
                    start_to_close_timeout=timedelta(seconds=20),
                    retry_policy=common.RetryPolicy(
                        initial_interval=timedelta(milliseconds=100),
                        maximum_attempts=2,
                    ),
                )
                for index in range(10)
            ]
        await asyncio.gather(*activities)


async def test_session_activity_and_retry_stay_on_workflow_worker(
    client: client_api.Client, other_client: client_api.Client
) -> None:
    @activity.defn
    async def retry_session_activity() -> None:
        if activity.info().attempt == 1:
            raise exceptions.ApplicationError("retry once")

    async with (
        new_worker(
            client,
            SessionRetryWorkflow,
            activities=[retry_session_activity],
            session_config=worker.WorkerSessionConfig(),
            disable_eager_activity_execution=True,
            max_concurrent_activities=2,
            sticky_queue_schedule_to_start_timeout=timedelta(minutes=1),
        ) as session_worker,
        new_worker(
            other_client,
            activities=[retry_session_activity],
            task_queue=session_worker.task_queue,
        ),
    ):
        await assert_task_queue_poller_eventually(
            client,
            session_worker.task_queue,
            other_client.identity,
            task_queue_type=enums.TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        )
        handle = await client.start_workflow(
            SessionRetryWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=session_worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        await handle.result()
        history = await handle.fetch_history()

        assert_workflow_and_activities_on_worker(
            history,
            client.identity,
            activity_ids={f"retry-{index}" for index in range(10)},
            activity_attempt=2,
        )
        assert all(
            event.activity_task_scheduled_event_attributes.session_options.HasField(
                "workflow_session"
            )
            for event in events_of_type(
                history, enums.EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            )
        )


@workflow.defn
class SessionBacklogWorkflow:
    @workflow.run
    async def run(self) -> None:
        with workflow.activity_session(IN_WORKFLOW_SESSION):
            activities = [
                workflow.start_activity(
                    "blocking_session_activity",
                    activity_id=f"held-{index}",
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=common.RetryPolicy(maximum_attempts=1),
                )
                for index in range(4)
            ]
        await asyncio.gather(*activities)


async def test_session_activity_backlog_waits_for_worker_capacity(
    client: client_api.Client, other_client: client_api.Client
) -> None:
    activity_started = asyncio.Event()
    release_activity = asyncio.Event()

    @activity.defn
    async def blocking_session_activity() -> None:
        activity_started.set()
        await release_activity.wait()

    async with (
        new_worker(
            client,
            SessionBacklogWorkflow,
            activities=[blocking_session_activity],
            session_config=worker.WorkerSessionConfig(),
            disable_eager_activity_execution=True,
            max_concurrent_activities=2,
            sticky_queue_schedule_to_start_timeout=timedelta(minutes=1),
        ) as session_worker,
        new_worker(
            other_client,
            activities=[blocking_session_activity],
            task_queue=session_worker.task_queue,
        ),
    ):
        await assert_task_queue_poller_eventually(
            client,
            session_worker.task_queue,
            other_client.identity,
            task_queue_type=enums.TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
        )
        handle = await client.start_workflow(
            SessionBacklogWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=session_worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        try:
            await asyncio.wait_for(activity_started.wait(), 10)

            async def assert_backlog() -> None:
                pending = (await handle.describe()).raw_description.pending_activities
                assert len(pending) == 4
                started = sum(
                    item.state
                    == enums.PendingActivityState.PENDING_ACTIVITY_STATE_STARTED
                    for item in pending
                )
                scheduled = sum(
                    item.state
                    == enums.PendingActivityState.PENDING_ACTIVITY_STATE_SCHEDULED
                    for item in pending
                )
                # A normal poll may reserve a slot, so full execution-slot utilization is not required.
                assert 1 <= started <= 2
                assert scheduled >= 2

            await assert_eventually(assert_backlog)
            release_activity.set()
            await handle.result()
        finally:
            release_activity.set()

        history = await handle.fetch_history()
        assert_workflow_and_activities_on_worker(
            history,
            client.identity,
            activity_ids={f"held-{index}" for index in range(4)},
        )


@activity.defn(name="replay_activity")
async def replay_activity(label: str) -> str:
    return label


@workflow.defn
class SessionReplayWorkflow:
    def __init__(self) -> None:
        self._proceed = False

    @workflow.run
    async def run(self, session_activities: bool) -> None:
        scope = (
            workflow.activity_session(IN_WORKFLOW_SESSION)
            if session_activities
            else contextlib.nullcontext()
        )
        with scope:
            await workflow.execute_activity(
                replay_activity, "first", start_to_close_timeout=timedelta(seconds=10)
            )
            # Lets the test replace the worker between activities
            await workflow.wait_condition(lambda: self._proceed)
            await workflow.execute_activity(
                replay_activity, "second", start_to_close_timeout=timedelta(seconds=10)
            )

    @workflow.signal
    def proceed(self) -> None:
        self._proceed = True


@pytest.mark.parametrize(
    "session_activities,single_session,replace_worker",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, False),
        (True, True, True),
    ],
    ids=[
        "plain-on-shared-worker",
        "session-on-shared-worker",
        "plain-on-single-session-worker",
        "session-on-single-session-worker",
        "session-on-replaced-single-session-worker",
    ],
)
async def test_session_workflow_histories_replay(
    client: client_api.Client,
    other_client: client_api.Client,
    session_activities: bool,
    single_session: bool,
    replace_worker: bool,
) -> None:
    session_config = worker.WorkerSessionConfig(
        admission=(
            worker.SingleSessionAdmission(stable_identity=f"replay-{uuid.uuid4()}")
            if single_session
            else None
        )
    )
    task_queue = str(uuid.uuid4())

    @contextlib.asynccontextmanager
    async def session_worker() -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(
                new_worker(
                    client,
                    SessionReplayWorkflow,
                    activities=[replay_activity],
                    task_queue=task_queue,
                    session_config=session_config,
                    disable_eager_activity_execution=True,
                )
            )
            if single_session and not session_activities:
                # A single-session worker polls only its session queue, so
                # non-session activities need another worker on the queue
                await stack.enter_async_context(
                    new_worker(
                        other_client,
                        activities=[replay_activity],
                        task_queue=task_queue,
                    )
                )
            yield

    handle: client_api.WorkflowHandle[Any, Any]
    async with session_worker():
        handle = await client.start_workflow(
            SessionReplayWorkflow.run,
            session_activities,
            id=str(uuid.uuid4()),
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=30),
        )

        async def first_activity_completed() -> None:
            assert events_of_type(
                await handle.fetch_history(),
                enums.EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED,
            )

        await assert_eventually(first_activity_completed)
        if not replace_worker:
            await handle.signal(SessionReplayWorkflow.proceed)
            await handle.result()
    if replace_worker:
        # A new worker with the same stable identity takes over the binding
        async with session_worker():
            await handle.signal(SessionReplayWorkflow.proceed)
            await handle.result()
    history = await handle.fetch_history()

    scheduled = events_of_type(
        history, enums.EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    )
    assert len(scheduled) == 2
    assert all(
        event.activity_task_scheduled_event_attributes.session_options.HasField(
            "workflow_session"
        )
        == session_activities
        for event in scheduled
    )

    # Replays with the session config it ran with
    await worker.Replayer(
        workflows=[SessionReplayWorkflow], session_config=session_config
    ).replay_workflow(history)
    # Without session config, only workflows without session activities replay
    result = await worker.Replayer(workflows=[SessionReplayWorkflow]).replay_workflow(
        history, raise_on_replay_failure=False
    )
    if session_activities:
        assert result.replay_failure is not None
        assert "session_worker_options is not enabled" in str(result.replay_failure)
    else:
        assert result.replay_failure is None
