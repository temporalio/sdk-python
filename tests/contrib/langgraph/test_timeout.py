from asyncio import sleep
from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from pytest import raises
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    await sleep(1)  # 1 second
    return {"value": "done"}


timeout_graph: StateGraph[State, None, State, State] = StateGraph(State)
timeout_graph.add_node("node", node)
timeout_graph.add_edge(START, "node")


@workflow.defn
class TimeoutWorkflow:
    def __init__(self) -> None:
        self.app = timeout_graph.compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        return await self.app.ainvoke({"value": input})


async def test_timeout(client: Client):
    task_queue = f"my-graph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[TimeoutWorkflow],
        plugins=[
            LangGraphPlugin(
                graphs=[timeout_graph],
                default_activity_options={
                    "start_to_close_timeout": timedelta(milliseconds=100),
                    "retry_policy": RetryPolicy(maximum_attempts=1),
                },
            )
        ],
    ):
        with raises(WorkflowFailureError):
            await client.execute_workflow(
                TimeoutWorkflow.run,
                "",
                id=f"test-workflow-{uuid4()}",
                task_queue=task_queue,
            )
