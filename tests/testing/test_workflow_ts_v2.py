"""Ports of time-skipping tests in testing/test_workflow.py"""

import uuid
from datetime import timedelta
from time import monotonic

from temporalio.testing import WorkflowEnvironment
from tests import DEV_SERVER_DOWNLOAD_VERSION
from tests.helpers import new_worker
from tests.helpers.time_skipping import (
    assert_time_was_not_skipped,
    assert_time_was_skipped,
)
from tests.testing.test_workflow import (
    SleepWorkflow,
    assert_timestamp_from_now,
)

_TS_EXTRA_ARGS = [
    "--dynamic-config-value",
    "frontend.TimeSkippingEnabled=true",
]


async def test_workflow_env_time_skipping_basic_v2():
    """Time-skip a very long sleep."""
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=_TS_EXTRA_ARGS,
    ) as env:
        async with new_worker(env.client, SleepWorkflow) as worker:
            handle = await env.client.start_workflow(
                SleepWorkflow.run,
                100000.0,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            result = await handle.result()
            assert result["message"] == "all done"
            assert_timestamp_from_now(
                await handle.query(SleepWorkflow.now), 100000
            )
            await assert_time_was_skipped(handle)


async def test_workflow_env_time_skipping_manual_v2():
    """Start a very long sleep, then fast forward the first 1000s."""
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=_TS_EXTRA_ARGS,
    ) as env:
        async with new_worker(env.client, SleepWorkflow) as worker:
            with env.with_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    SleepWorkflow.run,
                    100000.0,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )

            async def workflow_current_time() -> float:
                # Signal first so the next query timestamp is from a
                # non-query-only workflow task.
                await handle.signal(SleepWorkflow.tick)
                return await handle.query(SleepWorkflow.now)

            assert_timestamp_from_now(await workflow_current_time(), 0, max_delta=1)

            assert await env.fast_forward(handle, timedelta(seconds=1000))
            assert_timestamp_from_now(await workflow_current_time(), 1000)
            await assert_time_was_skipped(handle)

            await handle.cancel()


async def test_workflow_env_time_skipping_disabled_v2():
    """With and without per-workflow auto-skip."""
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=_TS_EXTRA_ARGS,
    ) as env:
        async with new_worker(env.client, SleepWorkflow) as worker:
            # With time-skipping enabled (env default), fast finish.
            start = monotonic()
            ts_on_handle = await env.client.start_workflow(
                SleepWorkflow.run,
                3.0,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            ts_on_result = await ts_on_handle.result()
            assert ts_on_result["message"] == "all done"
            assert monotonic() - start < 2.5
            await assert_time_was_skipped(ts_on_handle)

            # Without time-skipping, the workflow's 3s timer waits real
            # wall time.
            start = monotonic()
            with env.with_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    SleepWorkflow.run,
                    3.0,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
            ts_off_result = await handle.result()
            assert ts_off_result["message"] == "all done"
            assert monotonic() - start > 2.5
            await assert_time_was_not_skipped(handle)


async def test_workflow_env_time_skipping_basic_via_update_v2():
    """Start a very long sleep with time skipping off, then enable it
    and run to completion."""
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=_TS_EXTRA_ARGS,
    ) as env:
        async with new_worker(env.client, SleepWorkflow) as worker:
            with env.with_time_skipping_disabled():
                handle = await env.client.start_workflow(
                    SleepWorkflow.run,
                    100000.0,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )

            # Enable unbounded skipping mid-flight. Returns False because
            # the workflow terminates before a ``disabled_after_fast_forward``
            # transition can fire (there's no target).
            assert not await env.fast_forward(handle, None)
            result = await handle.result()
            assert result["message"] == "all done"
            assert_timestamp_from_now(
                await handle.query(SleepWorkflow.now), 100000
            )
            await assert_time_was_skipped(handle)
