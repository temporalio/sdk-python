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


async def node_a(state: State) -> dict[str, str]:
    return {"value": state["value"] + "a"}


async def node_b(state: State) -> dict[str, str]:
    return {"value": state["value"] + "b"}


my_graph: StateGraph[State, None, State, State] = StateGraph(State)
my_graph.add_node("node_a", node_a)
my_graph.add_node("node_b", node_b)
my_graph.add_edge(START, "node_a")
my_graph.add_edge("node_a", "node_b")


@workflow.defn
class TwoNodesWorkflow:
    def __init__(self) -> None:
        self.app = my_graph.compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        return await self.app.ainvoke({"value": input})


async def test_two_nodes(client: Client):
    task_queue = f"my-graph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[TwoNodesWorkflow],
        plugins=[
            LangGraphPlugin(
                graphs=[my_graph],
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
            )
        ],
    ):
        result = await client.execute_workflow(
            TwoNodesWorkflow.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "ab"}
