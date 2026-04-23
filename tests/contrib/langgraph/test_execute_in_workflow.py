from typing import Any
from uuid import uuid4

from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, graph
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    return {"value": "done"}


@workflow.defn
class ExecuteInWorkflowWorkflow:
    def __init__(self) -> None:
        self.app = graph("my-graph").compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        return await self.app.ainvoke({"value": input})


async def test_execute_in_workflow(client: Client):
    g = StateGraph(State)
    g.add_node("node", node, metadata={"execute_in": "workflow"})
    g.add_edge(START, "node")

    task_queue = f"my-graph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ExecuteInWorkflowWorkflow],
        plugins=[LangGraphPlugin(graphs={"my-graph": g})],
    ):
        result = await client.execute_workflow(
            ExecuteInWorkflowWorkflow.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "done"}
