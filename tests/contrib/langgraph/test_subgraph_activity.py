from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.graph import START, StateGraph

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin, graph
from temporalio.worker import Worker


async def child_node(_: str) -> str:
    return "child"


async def parent_node(state: str) -> str:
    child = StateGraph(str)
    child.add_node("child_node", child_node)
    child.add_edge(START, "child_node")

    return await child.compile().ainvoke(state)


@workflow.defn
class ActivitySubgraphWorkflow:
    @workflow.run
    async def run(self, input: str) -> Any:
        return await graph("parent").compile().ainvoke(input)


async def test_activity_subgraph(client: Client):
    parent = StateGraph(str)
    parent.add_node(
        "parent_node",
        parent_node,
        metadata={"start_to_close_timeout": timedelta(seconds=10)},
    )
    parent.add_edge(START, "parent_node")

    task_queue = f"subgraph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ActivitySubgraphWorkflow],
        plugins=[LangGraphPlugin(graphs={"parent": parent})],
    ):
        result = await client.execute_workflow(
            ActivitySubgraphWorkflow.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == "child"
