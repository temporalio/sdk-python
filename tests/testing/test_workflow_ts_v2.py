"""Time-skipping tests adapted to run against a per-workflow-TS-capable server.

These mirror the ``env.start_time_skipping()`` tests in ``test_workflow.py``
that have a meaningful per-workflow analog. Each spawns a per-test
NTS-enabled ``WorkflowEnvironment`` and uses ``env.fast_forward`` and
``env.with_time_skipping_disabled`` instead of the Java test server's
env-level sleep / clock / disable APIs.
"""

import uuid
from datetime import timedelta
from time import monotonic

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
