"""Time-skipping worker test adapted to run against a per-workflow-TS server.

Mirrors ``test_workflow_cancel_signal_and_timer_fired_in_same_task`` in
``test_workflow.py`` but uses per-workflow auto-skip on the NTS server
instead of the Java env's global ``env.sleep`` advances.
"""

import asyncio
import uuid

from temporalio.testing import WorkflowEnvironment
from tests import DEV_SERVER_DOWNLOAD_VERSION
from tests.helpers import new_worker
from tests.helpers.time_skipping import assert_time_skipping_engaged
from tests.worker.test_workflow import CancelSignalAndTimerFiredInSameTaskWorkflow


async def test_workflow_cancel_signal_and_timer_fired_in_same_task_v2():
    """Cancel-signal and timer fire delivered to the worker in the same task.

    The Java original explicitly advances the env clock to drive the 1h
    timer's fire while the worker is offline. The per-workflow equivalent
    enables auto-skipping; the server advances the workflow's virtual
    clock toward the timer fire whenever no work is in flight, including
    while the worker is down.
    """
    async with await WorkflowEnvironment.start_time_skipping_v2(
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
        dev_server_extra_args=[
            "--dynamic-config-value",
            "frontend.TimeSkippingEnabled=true",
        ],
    ) as env:
        async with new_worker(
            env.client,
            CancelSignalAndTimerFiredInSameTaskWorkflow,
            max_cached_workflows=0,
        ) as worker:
            task_queue = worker.task_queue
            handle = await env.client.start_workflow(
                CancelSignalAndTimerFiredInSameTaskWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            # Give the worker time to run the first WFT and schedule the
            # 1-hour timer before we shut it down.
            await asyncio.sleep(1)

        # Worker is down. Wait for the workflow result in the background so
        # the client keeps polling; send the cancel signal; auto-skipping
        # drives the timer fire while the worker is offline.
        result_task = asyncio.create_task(handle.result())
        await handle.signal(CancelSignalAndTimerFiredInSameTaskWorkflow.cancel_timer)

        async with new_worker(
            env.client,
            CancelSignalAndTimerFiredInSameTaskWorkflow,
            task_queue=task_queue,
            max_cached_workflows=0,
        ):
            # Previously this hung because a signal-driven cancel was not
            # respected after the timer fired.
            await result_task
            await assert_time_skipping_engaged(handle)
