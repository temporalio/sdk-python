from datetime import timedelta
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


async def child_node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    return {"value": "child"}


async def parent_node(state: State) -> dict[str, str]:
    child: StateGraph[State, None, State, State] = StateGraph(State)
    child.add_node("child_node", child_node)
    child.add_edge(START, "child_node")

    return await child.compile().ainvoke(state)


@workflow.defn
class ActivitySubgraphWorkflow:
    def __init__(self) -> None:
        self.app = graph("parent").compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        return await self.app.ainvoke({"value": input})


async def test_activity_subgraph(client: Client):
    parent = StateGraph(State)
    parent.add_node("parent_node", parent_node, metadata={"execute_in": "activity"})
    parent.add_edge(START, "parent_node")

    task_queue = f"subgraph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ActivitySubgraphWorkflow],
        plugins=[
            LangGraphPlugin(
                graphs={"parent": parent},
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
            )
        ],
    ):
        result = await client.execute_workflow(
            ActivitySubgraphWorkflow.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "child"}
