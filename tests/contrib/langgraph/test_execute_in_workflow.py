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


async def node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    return {"value": "done"}


inline_graph: StateGraph[State, None, State, State] = StateGraph(State)
inline_graph.add_node("node", node, metadata={"execute_in": "workflow"})
inline_graph.add_edge(START, "node")


@workflow.defn
class ExecuteInWorkflowWorkflow:
    def __init__(self) -> None:
        self.app = inline_graph.compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        return await self.app.ainvoke({"value": input})


async def test_execute_in_workflow(client: Client):
    task_queue = f"my-graph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ExecuteInWorkflowWorkflow],
        plugins=[LangGraphPlugin(graphs=[inline_graph])],
    ):
        result = await client.execute_workflow(
            ExecuteInWorkflowWorkflow.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "done"}
