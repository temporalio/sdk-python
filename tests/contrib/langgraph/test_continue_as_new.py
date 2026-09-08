from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import (  # pyright: ignore[reportMissingTypeStubs]
    RunnableConfig,
)
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, graph
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def node(state: State) -> dict[str, str]:
    return {"value": state["value"] + "a"}


@workflow.defn
class ContinueAsNewWorkflow:
    def __init__(self) -> None:
        self.app = graph("my-graph").compile(checkpointer=InMemorySaver())

    @workflow.run
    async def run(self, values: State) -> Any:
        config = RunnableConfig({"configurable": {"thread_id": "1"}})

        await self.app.aupdate_state(config, values)
        await self.app.ainvoke(values, config)

        if len(values["value"]) < 3:
            state = await self.app.aget_state(config)
            workflow.continue_as_new(state.values)

        return values


async def test_continue_as_new(client: Client):
    g = StateGraph(State)
    g.add_node("node", node, metadata={"execute_in": "activity"})
    g.add_edge(START, "node")

    task_queue = f"my-graph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ContinueAsNewWorkflow],
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
            ContinueAsNewWorkflow.run,
            State(value=""),
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "aaa"}
