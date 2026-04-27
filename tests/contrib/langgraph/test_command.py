from datetime import timedelta
from typing import Any, Literal
from uuid import uuid4

from langgraph.graph import (  # pyright: ignore[reportMissingTypeStubs]
    START,
    StateGraph,
)
from langgraph.types import Command
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, graph
from temporalio.worker import Worker


class State(TypedDict):
    value: str


def node_a(state: State) -> Command[Literal["node_b"]]:
    return Command(update={"value": state["value"] + "a"}, goto="node_b")


def node_b(state: State) -> Command[Literal["__end__"]]:
    return Command(update={"value": state["value"] + "b"}, goto="__end__")


@workflow.defn
class CommandWorkflow:
    def __init__(self) -> None:
        self.app = graph("my-graph").compile()

    @workflow.run
    async def run(self, input: str) -> Any:
        return await self.app.ainvoke({"value": input})


async def test_command_goto_and_update(client: Client):
    g = StateGraph(State)
    g.add_node("node_a", node_a, metadata={"execute_in": "activity"})
    g.add_node("node_b", node_b, metadata={"execute_in": "activity"})
    g.add_edge(START, "node_a")

    task_queue = f"command-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[CommandWorkflow],
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
            CommandWorkflow.run,
            "",
            id=f"test-command-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "ab"}
