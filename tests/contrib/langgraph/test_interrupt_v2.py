"""Test Graph API interrupt handling with version="v2".

With v2, ainvoke() returns a GraphOutput dataclass with .value and .interrupts
instead of mixing __interrupt__ into the state dict.
"""

from datetime import timedelta
from typing import Any
from uuid import uuid4

import langgraph.types
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import (  # pyright: ignore[reportMissingTypeStubs]
    RunnableConfig,
)
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin, graph
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    return {"value": langgraph.types.interrupt("Continue?")}


@workflow.defn
class InterruptV2Workflow:
    @workflow.run
    async def run(self, input: str) -> Any:
        g = graph("interrupt-v2-graph").compile(checkpointer=InMemorySaver())
        config = RunnableConfig({"configurable": {"thread_id": "1"}})

        result = await g.ainvoke({"value": input}, config, version="v2")

        # v2: interrupts are on result.interrupts, not result["__interrupt__"]
        assert result.value == {"value": ""}
        assert len(result.interrupts) == 1
        assert result.interrupts[0].value == "Continue?"

        return await g.ainvoke(langgraph.types.Command(resume="yes"), config)


async def test_interrupt_v2(client: Client):
    g = StateGraph(State)
    g.add_node("node", node)
    g.add_edge(START, "node")

    task_queue = f"interrupt-v2-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[InterruptV2Workflow],
        plugins=[
            LangGraphPlugin(
                graphs={"interrupt-v2-graph": g},
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
            )
        ],
    ):
        result = await client.execute_workflow(
            InterruptV2Workflow.run,
            "",
            id=f"test-interrupt-v2-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "yes"}
