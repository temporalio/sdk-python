import uuid
from collections.abc import Awaitable, Callable

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
            # A call after the Worker stops, as a plugin's cleanup makes.
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
    # The caller's own awaits after the block must not be cancelled.
    await client.count_workflows()
