"""Time-skipping tests adapted to run against a per-workflow-TS-capable server.

These mirror the ``env.start_time_skipping()`` tests in ``test_workflow.py``
that have a meaningful per-workflow analog. Each spawns a per-test
NTS-enabled ``WorkflowEnvironment`` and uses ``env.fast_forward`` and
``env.with_time_skipping_disabled`` instead of the Java test server's
env-level sleep / clock / disable APIs.
"""

import asyncio
import uuid
from datetime import timedelta
from time import monotonic

import pytest

from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, TimeoutError, TimeoutType
from temporalio.testing import WorkflowEnvironment
from tests import DEV_SERVER_DOWNLOAD_VERSION
from tests.helpers import new_worker
from tests.helpers.time_skipping import assert_time_skipping_engaged
from tests.testing.test_workflow import (
    ReallySlowWorkflow,
    ShortSleepWorkflow,
    assert_timestamp_from_now,
)

_TS_EXTRA_ARGS = [
    "--dynamic-config-value",
    "frontend.TimeSkippingEnabled=true",
]


async def test_workflow_env_time_skipping_basic_v2():
    """Per-workflow auto-skip drives the 100,000s sleep to completion.

    The Java original asserts on ``env.get_current_time()`` — NTS has no
    env-level clock, so we assert on the workflow's own virtual clock via
    its existing ``current_time`` query.
    """
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=_TS_EXTRA_ARGS,
    ) as env:
        async with new_worker(env.client, ReallySlowWorkflow) as worker:
            handle = await env.client.start_workflow(
                ReallySlowWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert "all done" == await handle.result()
            # Closed-workflow query: returns workflow.now() as of the final
            # workflow task, which is ~100,000s after start.
            assert_timestamp_from_now(
                await handle.query(ReallySlowWorkflow.current_time), 100000
            )
            await assert_time_skipping_engaged(handle)


async def test_workflow_env_time_skipping_manual_v2():
    """One-shot fast-forward of 1000s, asserted against the workflow's clock.

    The Java original advances the env clock via ``env.sleep(1000)``; the
    per-workflow equivalent is a ``env.fast_forward`` that auto-disables
    skipping once it completes.
    """
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=_TS_EXTRA_ARGS,
    ) as env:
        async with new_worker(env.client, ReallySlowWorkflow) as worker:
            with env.with_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    ReallySlowWorkflow.run,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )

            async def workflow_current_time() -> float:
                # Signal first so the next query timestamp is from a
                # non-query-only workflow task.
                await handle.signal(ReallySlowWorkflow.some_signal)
                return await handle.query(ReallySlowWorkflow.current_time)

            assert_timestamp_from_now(await workflow_current_time(), 0)

            assert await env.fast_forward(handle, timedelta(seconds=1000))
            assert_timestamp_from_now(await workflow_current_time(), 1000)
            await assert_time_skipping_engaged(handle)

            await handle.cancel()


async def test_workflow_env_time_skipping_disabled_v2():
    """With and without per-workflow auto-skip, same workflow timing.

    Maps onto the Java ``env.auto_time_skipping_disabled()`` toggle: use
    ``env.with_time_skipping_disabled()`` to start a workflow without
    TS stamping.
    """
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=_TS_EXTRA_ARGS,
    ) as env:
        async with new_worker(env.client, ShortSleepWorkflow) as worker:
            # With time-skipping enabled (env default), fast finish.
            start = monotonic()
            ts_on_handle = await env.client.start_workflow(
                ShortSleepWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            assert "all done" == await ts_on_handle.result()
            assert monotonic() - start < 2.5
            await assert_time_skipping_engaged(ts_on_handle)

            # Without time-skipping, the workflow's 3s timer waits real
            # wall time.
            start = monotonic()
            with env.with_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    ShortSleepWorkflow.run,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
            assert "all done" == await handle.result()
            assert monotonic() - start > 2.5


class HeartbeatTimeoutActivities:
    def __init__(self, env: WorkflowEnvironment) -> None:
        self.env = env

    @activity.defn
    async def fast_forward_own_workflow_and_wait(self, ff_seconds: float) -> str:
        info = activity.info()
        parent_handle = self.env.client.get_workflow_handle(
            info.workflow_id, run_id=info.workflow_run_id
        )
        await self.env.fast_forward(parent_handle, timedelta(seconds=ff_seconds))
        # Fast-forward advanced the workflow clock past heartbeat_timeout.
        await asyncio.Event().wait()
        return "unexpected: activity was not cancelled"


@workflow.defn
class HeartbeatTimeoutV2Workflow:
    @workflow.run
    async def run(self, ff_seconds: float) -> str:
        return await workflow.execute_activity_method(
            HeartbeatTimeoutActivities.fast_forward_own_workflow_and_wait,
            ff_seconds,
            schedule_to_close_timeout=timedelta(seconds=1000),
            heartbeat_timeout=timedelta(seconds=20),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


async def test_workflow_env_time_skipping_heartbeat_timeout_v2():
    """Activity-driven fast-forward pushes the workflow clock past heartbeat_timeout."""
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=_TS_EXTRA_ARGS,
    ) as env:
        activities = HeartbeatTimeoutActivities(env)
        async with new_worker(
            env.client,
            HeartbeatTimeoutV2Workflow,
            activities=[activities.fast_forward_own_workflow_and_wait],
        ) as worker:
            handle = await env.client.start_workflow(
                HeartbeatTimeoutV2Workflow.run,
                40.0,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            with pytest.raises(WorkflowFailureError) as err:
                await handle.result()
            assert isinstance(err.value.cause, ActivityError)
            assert isinstance(err.value.cause.cause, TimeoutError)
            assert err.value.cause.cause.type == TimeoutType.HEARTBEAT
            # Don't call assert_time_skipping_engaged(), since it didn't
            # get a chance to emit that.