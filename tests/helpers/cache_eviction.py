import asyncio
from datetime import timedelta

from temporalio import activity, workflow


@activity.defn
async def wait_forever_activity() -> None:
    await asyncio.Future()


@workflow.defn
class WaitForeverWorkflow:
    @workflow.run
    async def run(self) -> None:
        await asyncio.Future()


@workflow.defn
class CacheEvictionTearDownWorkflow:
    def __init__(self) -> None:
        self._signal_count = 0

    @workflow.run
    async def run(self) -> None:
        # Start several things in background. This is just to show that eviction
        # can work even with these things running.
        tasks = [
            asyncio.create_task(
                workflow.execute_activity(
                    wait_forever_activity, start_to_close_timeout=timedelta(hours=1)
                )
            ),
            asyncio.create_task(
                workflow.execute_child_workflow(WaitForeverWorkflow.run)
            ),
            asyncio.create_task(asyncio.sleep(1000)),
            asyncio.shield(
                workflow.execute_activity(
                    wait_forever_activity, start_to_close_timeout=timedelta(hours=1)
                )
            ),
            asyncio.create_task(workflow.wait_condition(lambda: False)),
        ]
        gather_fut = asyncio.gather(*tasks, return_exceptions=True)
        # Let's also start something in the background that we never wait on
        asyncio.create_task(asyncio.sleep(1000))
        try:
            print("----- in evict workflow")
            # Wait for signal count to reach 2
            await asyncio.sleep(0.01)
            print("----- waiting for signal")
            await workflow.wait_condition(lambda: self._signal_count > 1)
        finally:
            # This finally, on eviction, is actually called but the command
            # should be ignored
            print("----- sleeping")
            await asyncio.sleep(0.01)
            print("----- waiting for signals 2 & 3")
            await workflow.wait_condition(lambda: self._signal_count > 2)
            # Cancel gather tasks and wait on them, but ignore the errors
            for task in tasks:
                task.cancel()

            print("----- evict workflow ending")
            await gather_fut

    @workflow.signal
    async def signal(self) -> None:
        self._signal_count += 1

    @workflow.query
    def signal_count(self) -> int:
        return self._signal_count
