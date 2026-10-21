"""Opt-in SDK/server tests; see STICKY_ACTIVITY_INTEGRATION.md."""

import asyncio
import os
import uuid
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
        activities = [
            workflow.start_activity(
                "retry_session_activity",
                activity_id=f"retry-{index}",
                start_to_close_timeout=timedelta(seconds=20),
                session_options=workflow.ActivitySessionOptions.workflow_session(),
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
            session_worker_options=worker.WorkerSessionOptions(),
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
            event.activity_task_scheduled_event_attributes.session_options.use_workflow_session
            for event in events_of_type(
                history, enums.EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            )
        )


@workflow.defn
class SessionBacklogWorkflow:
    @workflow.run
    async def run(self) -> None:
        activities = [
            workflow.start_activity(
                "blocking_session_activity",
                activity_id=f"held-{index}",
                start_to_close_timeout=timedelta(seconds=30),
                session_options=workflow.ActivitySessionOptions.workflow_session(),
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
            session_worker_options=worker.WorkerSessionOptions(),
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
