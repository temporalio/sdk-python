"""
File used to test external filenames with __enhanced_stack_trace.
"""

import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from tests.helpers.external_coroutine import never_completing_coroutine, wait_on_timer


@activity.defn
async def external_wait_cancel() -> str:
    try:
        if activity.info().is_local:
            await asyncio.sleep(1000)
        else:
            while True:
                await asyncio.sleep(0.3)
                activity.heartbeat()
        return "Manually stopped"
    except asyncio.CancelledError:
        return "Got cancelled error, cancelled? " + str(activity.is_cancelled())


@workflow.defn
class ExternalStackTraceWorkflow:
    def __init__(self) -> None:
        self._status = ["created"]

    @workflow.run
    async def run(self) -> None:
        # Start several tasks
        self._status = ["spawning"]
        awaitables = [
            asyncio.sleep(1000),
            workflow.execute_activity(
                external_wait_cancel, schedule_to_close_timeout=timedelta(seconds=1000)
            ),
            never_completing_coroutine(self._status),
        ]
        await workflow.wait([asyncio.create_task(v) for v in awaitables])

    @workflow.query
    def status(self) -> str:
        return self._status[0]


@workflow.defn
class MultiFileStackTraceWorkflow:
    def __init__(self) -> None:
        self._status = ["created"]

    @workflow.run
    async def run_multifile_workflow(self) -> None:
        await wait_on_timer(self._status)

    @workflow.query
    def status(self) -> str:
        return self._status[0]
