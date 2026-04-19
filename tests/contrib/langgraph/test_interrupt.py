import sys
from datetime import timedelta
from typing import Any
from uuid import uuid4

import langgraph.types
import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="langgraph.types.interrupt() requires Python >= 3.11 for async context propagation",
)
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph.state import (  # pyright: ignore[reportMissingTypeStubs]
    RunnableConfig,
)
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    return {"value": langgraph.types.interrupt("Continue?")}


interrupt_graph: StateGraph[State, None, State, State] = StateGraph(State)
interrupt_graph.add_node("node", node)
interrupt_graph.add_edge(START, "node")


@workflow.defn
class InterruptWorkflow:
    def __init__(self) -> None:
        self.app = interrupt_graph.compile(checkpointer=InMemorySaver())

    @workflow.run
    async def run(self, input: str) -> Any:
        config = RunnableConfig({"configurable": {"thread_id": "1"}})

        result = await self.app.ainvoke({"value": input}, config)
        assert result["__interrupt__"][0].value == "Continue?"

        return await self.app.ainvoke(langgraph.types.Command(resume="yes"), config)


@workflow.defn
class InterruptV2Workflow:
    def __init__(self) -> None:
        self.app = interrupt_graph.compile(checkpointer=InMemorySaver())

    @workflow.run
    async def run(self, input: str) -> Any:
        config = RunnableConfig({"configurable": {"thread_id": "1"}})

        result = await self.app.ainvoke({"value": input}, config, version="v2")

        assert result.value == {"value": ""}
        assert len(result.interrupts) == 1
        assert result.interrupts[0].value == "Continue?"

        return await self.app.ainvoke(langgraph.types.Command(resume="yes"), config)


@pytest.mark.parametrize(
    "workflow_cls", [InterruptWorkflow, InterruptV2Workflow], ids=["v1", "v2"]
)
async def test_interrupt(client: Client, workflow_cls: Any) -> None:
    task_queue = f"interrupt-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[workflow_cls],
        plugins=[
            LangGraphPlugin(
                graphs=[interrupt_graph],
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
            )
        ],
    ):
        result = await client.execute_workflow(
            workflow_cls.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"value": "yes"}
