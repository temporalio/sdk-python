from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.graph import START, StateGraph
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin, graph


async def node_a(state: str) -> str:
    return state + "a"


async def node_b(state: str) -> str:
    return state + "b"


@workflow.defn
class TwoNodesWorkflow:
    @workflow.run
    async def run(self, input: str) -> Any:
        return await graph("my-graph").compile().ainvoke(input)


async def test_two_nodes(client: Client):
    g = StateGraph(str)
    g.add_node(
        "node_a",
        node_a,
        metadata={"start_to_close_timeout": timedelta(seconds=10)},
    )
    g.add_node(
        "node_b",
        node_b,
        metadata={"start_to_close_timeout": timedelta(seconds=10)},
    )
    g.add_edge(START, "node_a")
    g.add_edge("node_a", "node_b")

    task_queue = f"my-graph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[TwoNodesWorkflow],
        plugins=[LangGraphPlugin(graphs={"my-graph": g})],
    ):
        result = await client.execute_workflow(
            TwoNodesWorkflow.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == "ab"
