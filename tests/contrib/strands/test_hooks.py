from datetime import timedelta
from uuid import uuid4

from strands import Agent, tool
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin, TemporalModel
from temporalio.contrib.strands.workflow import activity_as_hook
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.common import get_activities
from tests.contrib.strands.mock_model import MockModel

# Module-level sink: written by the audit activity, read in assertions.
# Activity bodies run in worker context, not the sandbox, so a plain list is fine.
_AUDIT_LOG: list[str] = []


@activity.defn
async def audit_tool(tool_name: str) -> None:
    _AUDIT_LOG.append(tool_name)


@tool
def echo(text: str) -> str:
    return text


class AuditHook(HookProvider):
    def __init__(self) -> None:
        self.fired_events: list[str] = []

    def register_hooks(self, registry: HookRegistry, **kwargs: object) -> None:
        registry.add_callback(AfterToolCallEvent, self._sync_log)
        registry.add_callback(
            AfterToolCallEvent,
            activity_as_hook(
                audit_tool,
                activity_input=lambda event: event.tool_use["name"],
                start_to_close_timeout=timedelta(seconds=10),
            ),
        )

    def _sync_log(self, event: AfterToolCallEvent) -> None:
        # Deterministic in-workflow mutation: appends to per-workflow state.
        self.fired_events.append(event.tool_use["name"])


MODEL = TemporalModel(
    model_factory=lambda: MockModel(
        [
            {"name": "echo", "input": {"text": "hi"}},
            "Done!",
        ]
    ),
    start_to_close_timeout=timedelta(seconds=15),
)


@workflow.defn
class HooksWorkflow:
    def __init__(self) -> None:
        self.hook = AuditHook()
        self.agent = Agent(model=MODEL, tools=[echo], hooks=[self.hook])

    @workflow.run
    async def run(self, prompt: str) -> list[str]:
        await self.agent.invoke_async(prompt)
        return self.hook.fired_events


async def test_hooks(client: Client):
    _AUDIT_LOG.clear()
    task_queue = "test_hooks"
    plugin = StrandsPlugin(model=MODEL)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[HooksWorkflow],
        activities=[audit_tool],
        plugins=[plugin],
    ):
        handle = await client.start_workflow(
            HooksWorkflow.run,
            "Say hi",
            id=f"test_hooks_{uuid4()}",
            task_queue=task_queue,
        )
        assert await handle.result() == ["echo"]

    assert _AUDIT_LOG == ["echo"]

    history = await handle.fetch_history()
    assert "audit_tool" in get_activities(history)

    await Replayer(
        workflows=[HooksWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)
