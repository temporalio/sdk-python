"""Activity session tests.

Most run against the stock test server, which can't route session activities,
so placement is observed on ``StartActivityInput.session_options`` by an
interceptor that then clears it. Tests marked ``requires_session_server`` need a
session-capable server; see STICKY_ACTIVITY_INTEGRATION.md.
"""

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest

from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType, PendingActivityState, TaskQueueType
from temporalio.client import Client, WorkflowHandle
from temporalio.common import ActivitySessionOptions, RetryPolicy, UseWorkflowSession
from temporalio.exceptions import ApplicationError
from temporalio.worker import (
    Interceptor,
    Replayer,
    SingleSessionAdmission,
    StartActivityInput,
    WorkerSessionConfig,
    WorkerTuner,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)
from tests.helpers import (
    assert_eq_eventually,
    assert_eventually,
    assert_task_queue_poller_eventually,
    assert_workflow_and_activities_on_worker,
    events_of_type,
    new_worker,
)

IN_SESSION = ActivitySessionOptions(placement=UseWorkflowSession())

requires_session_server = pytest.mark.skipif(
    os.getenv("TEMPORAL_TEST_STICKY_ACTIVITIES") != "1",
    reason="requires a session-capable server and namespace gate",
)

# Resolved session options per workflow ID and activity label
resolved_session_options: dict[str, dict[str, ActivitySessionOptions | None]] = {}


@activity.defn
async def session_test_activity(label: str) -> str:
    return label


class RecordSessionInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        return RecordSessionInboundInterceptor


class RecordSessionInboundInterceptor(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(RecordSessionOutboundInterceptor(outbound))


class RecordSessionOutboundInterceptor(WorkflowOutboundInterceptor):
    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle:
        resolved_session_options.setdefault(workflow.info().workflow_id, {})[
            input.args[0]
        ] = input.session_options
        input.session_options = None
        return super().start_activity(input)


async def run_labeled(label: str, task_queue: str | None = None) -> None:
    await workflow.execute_activity(
        session_test_activity,
        label,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=30),
    )


@workflow.defn
class SessionScopeWorkflow:
    def __init__(self) -> None:
        self._release_task = asyncio.Event()
        self._signals_handled = 0

    @workflow.run
    async def run(self, other_task_queue: str) -> None:
        with workflow.activity_session(IN_SESSION):
            await run_labeled("in_scope")
            await run_labeled("in_scope_explicit_queue", workflow.info().task_queue)
            await run_labeled("in_scope_other_queue", other_task_queue)
            with workflow.activity_session(IN_SESSION):
                await run_labeled("nested_scope")
            await run_labeled("after_nested_scope")
            task = asyncio.create_task(self._run_after_release())
        await run_labeled("after_scope")
        self._release_task.set()
        await task
        await workflow.wait_condition(lambda: self._signals_handled == 2)

    async def _run_after_release(self) -> None:
        await self._release_task.wait()
        await run_labeled("task_created_in_scope")

    @workflow.signal
    async def unscoped_signal(self) -> None:
        await run_labeled("unscoped_signal")
        self._signals_handled += 1

    @workflow.signal
    async def scoped_signal(self) -> None:
        with workflow.activity_session(IN_SESSION):
            await run_labeled("scoped_signal")
        self._signals_handled += 1


async def test_activity_session_scope_resolution(client: Client) -> None:
    other_task_queue = f"other-{uuid.uuid4()}"
    async with (
        new_worker(
            client,
            SessionScopeWorkflow,
            activities=[session_test_activity],
            interceptors=[RecordSessionInterceptor()],
        ) as worker,
        new_worker(
            client,
            activities=[session_test_activity],
            task_queue=other_task_queue,
        ),
    ):
        handle = await client.start_workflow(
            SessionScopeWorkflow.run,
            other_task_queue,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.signal(SessionScopeWorkflow.unscoped_signal)
        await handle.signal(SessionScopeWorkflow.scoped_signal)
        await handle.result()

    assert resolved_session_options.pop(handle.id) == {
        "in_scope": IN_SESSION,
        "in_scope_explicit_queue": IN_SESSION,
        "in_scope_other_queue": None,
        "nested_scope": IN_SESSION,
        "after_nested_scope": IN_SESSION,
        "after_scope": None,
        "task_created_in_scope": IN_SESSION,
        "unscoped_signal": None,
        "scoped_signal": IN_SESSION,
    }


@workflow.defn
class SingleScopedActivityWorkflow:
    @workflow.run
    async def run(self) -> None:
        with workflow.activity_session(IN_SESSION):
            await run_labeled("in_scope")


async def last_workflow_task_failure(handle: WorkflowHandle) -> str:
    async for event in handle.fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED:
            return event.workflow_task_failed_event_attributes.failure.message
    return "<no failure>"


async def test_activity_session_requires_session_worker(client: Client) -> None:
    # Core rejects the session activity; workflow code can't catch it
    async with new_worker(
        client, SingleScopedActivityWorkflow, activities=[session_test_activity]
    ) as worker:
        handle = await client.start_workflow(
            SingleScopedActivityWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        async def session_worker_failure() -> bool:
            return "session_worker_options is not enabled" in (
                await last_workflow_task_failure(handle)
            )

        await assert_eq_eventually(True, session_worker_failure)
        await handle.terminate()


def test_activity_session_outside_workflow() -> None:
    with pytest.raises(workflow._NotInWorkflowEventLoopError):
        with workflow.activity_session(IN_SESSION):
            pass


async def test_session_worker_requires_workflow_cache(client: Client) -> None:
    with pytest.raises(Exception, match="nonzero workflow cache"):
        new_worker(
            client,
            SingleScopedActivityWorkflow,
            activities=[session_test_activity],
            max_cached_workflows=0,
            session_config=WorkerSessionConfig(),
        )


async def test_single_session_requires_stable_identity(client: Client) -> None:
    with pytest.raises(Exception, match="stable_identity must be nonempty"):
        new_worker(
            client,
            SingleScopedActivityWorkflow,
            activities=[session_test_activity],
            session_config=WorkerSessionConfig(
                admission=SingleSessionAdmission(stable_identity="")
            ),
            tuner=WorkerTuner.create_fixed(activity_slots=1),
        )


@pytest.mark.parametrize("single_session", [False, True])
async def test_single_session_worker_skips_eager_start(
    client: Client, single_session: bool
) -> None:
    # Eager start bypasses polling, so a single-session worker would skip binding
    session_config = WorkerSessionConfig(
        admission=(
            SingleSessionAdmission(stable_identity=f"session-{uuid.uuid4()}")
            if single_session
            else None
        )
    )
    async with new_worker(
        client,
        SingleScopedActivityWorkflow,
        activities=[session_test_activity],
        session_config=session_config,
        tuner=WorkerTuner.create_fixed(activity_slots=2),
    ) as worker:
        handle = await client.start_workflow(
            SingleScopedActivityWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            request_eager_start=True,
        )
        assert handle.__temporal_eagerly_started is not single_session
        await handle.terminate()


@pytest.fixture
async def other_client(client: Client, env_type: str) -> Client:
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
    return await Client.connect(**config)


@workflow.defn
class SessionRetryWorkflow:
    @workflow.run
    async def run(self) -> None:
        with workflow.activity_session(IN_SESSION):
            activities = [
                workflow.start_activity(
                    "retry_session_activity",
                    activity_id=f"retry-{index}",
                    start_to_close_timeout=timedelta(seconds=20),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(milliseconds=100),
                        maximum_attempts=2,
                    ),
                )
                for index in range(10)
            ]
        await asyncio.gather(*activities)


@requires_session_server
async def test_session_activity_and_retry_stay_on_workflow_worker(
    client: Client, other_client: Client
) -> None:
    @activity.defn
    async def retry_session_activity() -> None:
        if activity.info().attempt == 1:
            raise ApplicationError("retry once")

    async with (
        new_worker(
            client,
            SessionRetryWorkflow,
            activities=[retry_session_activity],
            session_config=WorkerSessionConfig(),
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
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
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
                history, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            )
        )


@workflow.defn
class SessionBacklogWorkflow:
    @workflow.run
    async def run(self) -> None:
        with workflow.activity_session(IN_SESSION):
            activities = [
                workflow.start_activity(
                    "blocking_session_activity",
                    activity_id=f"held-{index}",
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                for index in range(4)
            ]
        await asyncio.gather(*activities)


@requires_session_server
async def test_session_activity_backlog_waits_for_worker_capacity(
    client: Client, other_client: Client
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
            session_config=WorkerSessionConfig(),
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
            task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
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
                    item.state == PendingActivityState.PENDING_ACTIVITY_STATE_STARTED
                    for item in pending
                )
                scheduled = sum(
                    item.state == PendingActivityState.PENDING_ACTIVITY_STATE_SCHEDULED
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
            workflow.activity_session(IN_SESSION)
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


@requires_session_server
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
    client: Client,
    other_client: Client,
    session_activities: bool,
    single_session: bool,
    replace_worker: bool,
) -> None:
    session_config = WorkerSessionConfig(
        admission=(
            SingleSessionAdmission(stable_identity=f"replay-{uuid.uuid4()}")
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

    handle: WorkflowHandle[Any, Any]
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
                EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED,
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

    scheduled = events_of_type(history, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    assert len(scheduled) == 2
    assert all(
        event.activity_task_scheduled_event_attributes.session_options.HasField(
            "workflow_session"
        )
        == session_activities
        for event in scheduled
    )

    # Replays with the session config it ran with
    await Replayer(
        workflows=[SessionReplayWorkflow], session_config=session_config
    ).replay_workflow(history)
    # Without session config, only workflows without session activities replay
    result = await Replayer(workflows=[SessionReplayWorkflow]).replay_workflow(
        history, raise_on_replay_failure=False
    )
    if session_activities:
        assert result.replay_failure is not None
        assert "session_worker_options is not enabled" in str(result.replay_failure)
    else:
        assert result.replay_failure is None
