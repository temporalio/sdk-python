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


def sync_node(state: State) -> dict[str, str]:
    return {"value": state["value"] + "!"}


@workflow.defn
class SyncNodeWorkflow:
    def __init__(self) -> None:
        self.app = graph("my-graph").compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        return await self.app.ainvoke({"value": input})


async def test_sync_node(client: Client):
    g = StateGraph(State)
    g.add_node("sync_node", sync_node, metadata={"execute_in": "activity"})
    g.add_edge(START, "sync_node")

    task_queue = f"sync-node-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SyncNodeWorkflow],
        plugins=[
            LangGraphPlugin(
                graphs={"my-graph": g},
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
            )
        ],
    ):
        result = await client.execute_workflow(
            SyncNodeWorkflow.run,
            "hello",
            id=f"test-sync-node-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "hello!"}
