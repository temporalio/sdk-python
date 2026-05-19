from datetime import timedelta
from uuid import uuid4

from strands import Agent, tool
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent
from strands.types.interrupt import InterruptResponseContent

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin, TemporalModel
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.common import get_activities
from tests.contrib.strands.mock_model import MockModel


@tool
def delete_thing(name: str) -> str:
    return f"deleted {name}"


class ApprovalHook(HookProvider):
    def register_hooks(self, registry: HookRegistry, **kwargs: object) -> None:
        registry.add_callback(BeforeToolCallEvent, self._gate)

    def _gate(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != "delete_thing":
            return
        approval = event.interrupt(
            "approval",
            reason=f"approve delete of {event.tool_use['input']['name']}?",
        )
        if approval != "approve":
            event.cancel_tool = "denied"


@workflow.defn
class InterruptWorkflow:
    def __init__(self) -> None:
        model = TemporalModel(
            model_name="mock",
            start_to_close_timeout=timedelta(seconds=15),
        )
        self.agent = Agent(model=model, tools=[delete_thing], hooks=[ApprovalHook()])
        self._approval: str | None = None

    @workflow.signal
    def approve(self, response: str) -> None:
        self._approval = response

    @workflow.run
    async def run(self, prompt: str) -> str:
        result = await self.agent.invoke_async(prompt)
        while result.stop_reason == "interrupt":
            await workflow.wait_condition(lambda: self._approval is not None)
            response = self._approval
            self._approval = None
            responses: list[InterruptResponseContent] = [
                {"interruptResponse": {"interruptId": i.id, "response": response}}
                for i in (result.interrupts or [])
            ]
            result = await self.agent.invoke_async(responses)
        return str(result)


async def test_interrupt(client: Client):
    task_queue = "test_interrupt"
    plugin = StrandsPlugin(
        models={
            "mock": lambda: MockModel(
                [
                    {"name": "delete_thing", "input": {"name": "foo"}},
                    "Done!",
                ]
            )
        }
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[InterruptWorkflow],
        plugins=[plugin],
    ):
        handle = await client.start_workflow(
            InterruptWorkflow.run,
            "delete foo",
            id=f"test_interrupt_{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(InterruptWorkflow.approve, "approve")
        assert await handle.result() == "Done!\n"

    history = await handle.fetch_history()
    assert get_activities(history) == [
        "invoke_model",
        "invoke_model",
    ]

    await Replayer(
        workflows=[InterruptWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)
