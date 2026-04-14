from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import RunnableConfig
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin, graph
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def node(state: State) -> dict[str, str]:
    return {"value": interrupt("Continue?")}


@workflow.defn
class InterruptWorkflow:
    @workflow.run
    async def run(self, input: str) -> Any:
        g = graph("my-graph").compile(checkpointer=InMemorySaver())
        config = RunnableConfig({"configurable": {"thread_id": "1"}})

        result = await g.ainvoke({"value": input}, config)
        assert result["__interrupt__"][0].value == "Continue?"

        return await g.ainvoke(Command(resume="yes"), config)


async def test_interrupt(client: Client):
    g = StateGraph(State)
    g.add_node(
        "node",
        node,
        metadata={"start_to_close_timeout": timedelta(seconds=10)},
    )
    g.add_edge(START, "node")

    task_queue = f"my-graph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[InterruptWorkflow],
        plugins=[LangGraphPlugin(graphs={"my-graph": g})],
    ):
        result = await client.execute_workflow(
            InterruptWorkflow.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "yes"}
