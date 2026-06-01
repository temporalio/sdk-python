from datetime import timedelta
from uuid import uuid4

from strands import tool
from strands.interrupt import Interrupt, InterruptException
from strands.types.interrupt import InterruptResponseContent

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.strands import StrandsPlugin, TemporalAgent
from temporalio.contrib.strands.workflow import activity_as_tool
from temporalio.worker import Replayer, Worker
from tests.contrib.strands.common import get_activities
from tests.contrib.strands.mock_model import MockModel


@tool
def in_workflow_delete(name: str) -> str:
    raise InterruptException(
        Interrupt(id=f"delete:{name}", name="approval", reason=f"delete {name}?")
    )


# Counts attempts so the activity raises on the first invocation and succeeds on
# the second — modeling a real "approval flipped an external flag" check.
_activity_delete_calls = 0


@activity.defn
async def activity_delete(name: str) -> str:
    global _activity_delete_calls
    _activity_delete_calls += 1
    if _activity_delete_calls == 1:
        raise InterruptException(
            Interrupt(id=f"delete:{name}", name="approval", reason=f"delete {name}?")
        )
    return f"deleted {name}"


@workflow.defn
class InWorkflowToolInterruptWorkflow:
    def __init__(self) -> None:
        self.agent = TemporalAgent(
            model="mock",
            start_to_close_timeout=timedelta(seconds=15),
            tools=[in_workflow_delete],
        )
        self._approval: str | None = None

    @workflow.signal
    def approve(self, response: str) -> None:
        self._approval = response

    @workflow.run
    async def run(self, prompt: str) -> str:
        result = await self.agent.invoke_async(prompt)
        while result.stop_reason == "interrupt":
            await workflow.wait_condition(lambda: self._approval is not None)
            response, self._approval = self._approval, None
            responses: list[InterruptResponseContent] = [
                {"interruptResponse": {"interruptId": i.id, "response": response}}
                for i in (result.interrupts or [])
            ]
            result = await self.agent.invoke_async(responses)
        return str(result)


@workflow.defn
class ActivityToolInterruptWorkflow:
    def __init__(self) -> None:
        self.agent = TemporalAgent(
            model="mock",
            start_to_close_timeout=timedelta(seconds=15),
            tools=[
                activity_as_tool(
                    activity_delete,
                    start_to_close_timeout=timedelta(seconds=15),
                )
            ],
        )
        self._approval: str | None = None

    @workflow.signal
    def approve(self, response: str) -> None:
        self._approval = response

    @workflow.run
    async def run(self, prompt: str) -> str:
        result = await self.agent.invoke_async(prompt)
        while result.stop_reason == "interrupt":
            await workflow.wait_condition(lambda: self._approval is not None)
            response, self._approval = self._approval, None
            responses: list[InterruptResponseContent] = [
                {"interruptResponse": {"interruptId": i.id, "response": response}}
                for i in (result.interrupts or [])
            ]
            result = await self.agent.invoke_async(responses)
        return str(result)


async def test_in_workflow_tool_interrupt(client: Client):
    task_queue = "test_in_workflow_tool_interrupt"
    plugin = StrandsPlugin(
        models={
            "mock": lambda: MockModel(
                [
                    {"name": "in_workflow_delete", "input": {"name": "foo"}},
                    "Done!",
                ]
            )
        }
    )

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[InWorkflowToolInterruptWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            InWorkflowToolInterruptWorkflow.run,
            "delete foo",
            id=f"test_in_workflow_tool_interrupt_{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(InWorkflowToolInterruptWorkflow.approve, "approve")
        assert await handle.result() == "Done!\n"

    history = await handle.fetch_history()
    # No activity call for the in-workflow @tool — only model calls.
    assert get_activities(history) == ["invoke_model", "invoke_model"]

    await Replayer(
        workflows=[InWorkflowToolInterruptWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)


async def test_activity_tool_interrupt(client: Client):
    global _activity_delete_calls
    _activity_delete_calls = 0
    task_queue = "test_activity_tool_interrupt"
    plugin = StrandsPlugin(
        models={
            "mock": lambda: MockModel(
                [
                    {"name": "activity_delete", "input": {"name": "foo"}},
                    "Done!",
                ]
            )
        }
    )

    # Activity-side InterruptException relies on the failure converter installed
    # via the data converter, which _ActivityWorker reads from the client config.
    # Re-create the client with the plugin attached so that converter takes effect.
    config = client.config()
    config["plugins"] = [*config["plugins"], plugin]
    client = Client(**config)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ActivityToolInterruptWorkflow],
        activities=[activity_delete],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            ActivityToolInterruptWorkflow.run,
            "delete foo",
            id=f"test_activity_tool_interrupt_{uuid4()}",
            task_queue=task_queue,
        )
        await handle.signal(ActivityToolInterruptWorkflow.approve, "approve")
        assert await handle.result() == "Done!\n"

    history = await handle.fetch_history()
    # activity_delete appears twice: once for the call that raised
    # InterruptException, once for the resume call that returned successfully.
    assert get_activities(history) == [
        "invoke_model",
        "activity_delete",
        "activity_delete",
        "invoke_model",
    ]

    await Replayer(
        workflows=[ActivityToolInterruptWorkflow],
        plugins=[plugin],
    ).replay_workflow(history)
