"""Activity session tests that run against the stock test server.

The stock server can't route session activities, so placement is observed on
``StartActivityInput.session_options`` by an interceptor that then clears it.
Server-backed routing is covered by test_activity_sessions_integration.py.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHandle
from temporalio.common import ActivitySessionOptions, UseWorkflowSession
from temporalio.worker import (
    Interceptor,
    SingleSessionAdmission,
    StartActivityInput,
    WorkerSessionConfig,
    WorkerTuner,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)
from tests.helpers import assert_eq_eventually, new_worker

IN_SESSION = ActivitySessionOptions(placement=UseWorkflowSession())

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
