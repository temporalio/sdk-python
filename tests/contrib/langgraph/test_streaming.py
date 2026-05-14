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


async def node_a(state: State) -> dict[str, str]:
    return {"value": state["value"] + "a"}


async def node_b(state: State) -> dict[str, str]:
    return {"value": state["value"] + "b"}


@workflow.defn
class StreamingWorkflow:
    def __init__(self) -> None:
        self.app = graph("streaming").compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        chunks = []
        async for chunk in self.app.astream({"value": input}):
            chunks.append(chunk)
        return chunks


async def test_streaming(client: Client):
    g = StateGraph(State)
    g.add_node("node_a", node_a, metadata={"execute_in": "activity"})
    g.add_node("node_b", node_b, metadata={"execute_in": "activity"})
    g.add_edge(START, "node_a")
    g.add_edge("node_a", "node_b")

    task_queue = f"streaming-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[StreamingWorkflow],
        plugins=[
            LangGraphPlugin(
                graphs={"streaming": g},
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
            )
        ],
    ):
        chunks = await client.execute_workflow(
            StreamingWorkflow.run,
            "",
            id=f"test-streaming-{uuid4()}",
            task_queue=task_queue,
        )

    assert chunks == [{"node_a": {"value": "a"}}, {"node_b": {"value": "ab"}}]
