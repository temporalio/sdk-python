"""Test Graph API interrupt handling with version="v2".

With v2, ainvoke() returns a GraphOutput dataclass with .value and .interrupts
instead of mixing __interrupt__ into the state dict.
"""

from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import RunnableConfig
from langgraph.types import Command, interrupt
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin, graph


async def node(_: str) -> str:
    return interrupt("Continue?")


@workflow.defn
class InterruptV2Workflow:
    @workflow.run
    async def run(self, input: str) -> Any:
        g = graph("interrupt-v2-graph").compile(
            checkpointer=InMemorySaver()
        )
        config = RunnableConfig(
            {"configurable": {"thread_id": "1"}}
        )

        result = await g.ainvoke(input, config, version="v2")

        # v2: interrupts are on result.interrupts, not result["__interrupt__"]
        assert result.value == {}
        assert len(result.interrupts) == 1
        assert result.interrupts[0].value == "Continue?"

        return await g.ainvoke(Command(resume="yes"), config)


async def test_interrupt_v2(client: Client):
    g = StateGraph(str)
    g.add_node(
        "node",
        node,
        metadata={"start_to_close_timeout": timedelta(seconds=10)},
    )
    g.add_edge(START, "node")

    task_queue = f"interrupt-v2-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[InterruptV2Workflow],
        plugins=[LangGraphPlugin(graphs={"interrupt-v2-graph": g})],
    ):
        result = await client.execute_workflow(
            InterruptV2Workflow.run,
            "",
            id=f"test-interrupt-v2-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == "yes"
