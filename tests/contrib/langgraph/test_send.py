import operator
from datetime import timedelta
from typing import Annotated, Any
from uuid import uuid4

from langgraph.graph import (  # pyright: ignore[reportMissingTypeStubs]
    END,
    START,
    StateGraph,
)
from langgraph.types import Send
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, graph
from temporalio.worker import Worker


class State(TypedDict):
    items: list[str]
    results: Annotated[list[str], operator.add]


class WorkerState(TypedDict):
    item: str


def worker(state: WorkerState) -> dict[str, list[str]]:
    return {"results": [state["item"].upper()]}


async def fan_out(state: State) -> list[Send]:
    return [Send("worker", {"item": item}) for item in state["items"]]


@workflow.defn
class SendWorkflow:
    def __init__(self) -> None:
        self.app = graph("my-graph").compile()

    @workflow.run
    async def run(self, items: list[str]) -> Any:
        return await self.app.ainvoke({"items": items, "results": []})


async def test_send(client: Client):
    g = StateGraph(State)
    g.add_node("worker", worker, metadata={"execute_in": "activity"})
    g.add_conditional_edges(START, fan_out, ["worker"])
    g.add_edge("worker", END)

    task_queue = f"send-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SendWorkflow],
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
            SendWorkflow.run,
            ["a", "b", "c"],
            id=f"test-send-{uuid4()}",
            task_queue=task_queue,
        )

    assert result["items"] == ["a", "b", "c"]
    assert sorted(result["results"]) == ["A", "B", "C"]
