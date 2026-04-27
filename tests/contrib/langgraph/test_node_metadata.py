from datetime import timedelta
from typing import Any
from uuid import uuid4

from langchain_core.runnables import (
    RunnableConfig,  # pyright: ignore[reportMissingTypeStubs]
)
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def node(state: State, config: RunnableConfig) -> dict[str, str]:
    metadata = config.get("metadata") or {}
    return {"value": state["value"] + str(metadata.get("my_key", "NOT_FOUND"))}


metadata_graph: StateGraph[State, None, State, State] = StateGraph(State)
metadata_graph.add_node(
    "node",
    node,
    metadata={
        "execute_in": "activity",
        "start_to_close_timeout": timedelta(seconds=10),
        "my_key": "my_value",
    },
)
metadata_graph.add_edge(START, "node")


@workflow.defn
class NodeMetadataWorkflow:
    def __init__(self) -> None:
        self.app = metadata_graph.compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        return await self.app.ainvoke({"value": input})


async def test_node_metadata_readable_in_node(client: Client):
    task_queue = f"my-graph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[NodeMetadataWorkflow],
        plugins=[LangGraphPlugin(graphs={"my-graph": metadata_graph})],
    ):
        result = await client.execute_workflow(
            NodeMetadataWorkflow.run,
            "prefix-",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "prefix-my_value"}
