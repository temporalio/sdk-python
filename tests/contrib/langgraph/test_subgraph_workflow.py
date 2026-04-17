from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def child_node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    return {"value": "child"}


child_graph: StateGraph[State, None, State, State] = StateGraph(State)
child_graph.add_node(
    "child_node",
    child_node,
    metadata={"start_to_close_timeout": timedelta(seconds=10)},
)
child_graph.add_edge(START, "child_node")


async def parent_node(state: State) -> dict[str, str]:
    return await child_graph.compile().ainvoke(state)


parent_graph: StateGraph[State, None, State, State] = StateGraph(State)
parent_graph.add_node("parent_node", parent_node, metadata={"execute_in": "workflow"})
parent_graph.add_edge(START, "parent_node")


@workflow.defn
class WorkflowSubgraphWorkflow:
    def __init__(self) -> None:
        self.app = parent_graph.compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        return await self.app.ainvoke({"value": input})


async def test_workflow_subgraph(client: Client):
    task_queue = f"subgraph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[WorkflowSubgraphWorkflow],
        plugins=[LangGraphPlugin(graphs=[parent_graph, child_graph])],
    ):
        result = await client.execute_workflow(
            WorkflowSubgraphWorkflow.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "child"}
