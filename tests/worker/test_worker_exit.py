import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

from temporalio import workflow
from temporalio.client import Client
from temporalio.plugin import SimplePlugin
from temporalio.worker import Worker


@workflow.defn
class NothingWorkflow:
    @workflow.run
    async def run(self) -> None:
        pass


class CleanupPlugin(SimplePlugin):
    def __init__(self, client: Client) -> None:
        super().__init__("test-cleanup")
        self.client = client
        self.cleanup_result: int | None = None

    async def run_worker(
        self, worker: Worker, next: Callable[[Worker], Awaitable[None]]
    ) -> None:
        try:
            await super().run_worker(worker, next)
        finally:
            # Plugin cleanup code calls the server after the Worker stops.
            self.cleanup_result = (await self.client.count_workflows()).count


async def test_plugin_cleanup_awaits_after_worker_exit(client: Client):
    plugin = CleanupPlugin(client)
    async with Worker(
        client,
        task_queue=f"tq-{uuid.uuid4()}",
        workflows=[NothingWorkflow],
        plugins=[plugin],
    ):
        pass
    assert plugin.cleanup_result is not None
    # The caller can still await after the block.
    await client.count_workflows()


class StuckCleanupPlugin(SimplePlugin):
    def __init__(self) -> None:
        super().__init__("test-stuck-cleanup")
        self.cancelled = False

    async def run_worker(
        self, worker: Worker, next: Callable[[Worker], Awaitable[None]]
    ) -> None:
        try:
            await super().run_worker(worker, next)
        finally:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise


async def test_plugin_cleanup_is_cancelled_after_plugin_shutdown_timeout(
    client: Client,
):
    plugin = StuckCleanupPlugin()
    async with Worker(
        client,
        task_queue=f"tq-{uuid.uuid4()}",
        workflows=[NothingWorkflow],
        plugins=[plugin],
        plugin_shutdown_timeout=timedelta(milliseconds=100),
    ):
        pass
    # Let the cancelled run task finish its cleanup.
    await asyncio.sleep(0)
    assert plugin.cancelled
