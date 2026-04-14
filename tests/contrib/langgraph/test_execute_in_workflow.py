from typing import Any
from uuid import uuid4

from langgraph.graph import START, StateGraph
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin, graph


async def node(_: str) -> str:
    return "done"


@workflow.defn
class ExecuteInWorkflowWorkflow:
    @workflow.run
    async def run(self, input: str) -> Any:
        return await graph("my-graph").compile().ainvoke(input)


async def test_execute_in_workflow(client: Client):
    g = StateGraph(str)
    g.add_node("node", node, metadata={"execute_in": "workflow"})
    g.add_edge(START, "node")

    task_queue = f"my-graph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ExecuteInWorkflowWorkflow],
        plugins=[LangGraphPlugin(graphs={"my-graph": g})],
    ):
        result = await client.execute_workflow(
            ExecuteInWorkflowWorkflow.run,
            "",
            id=f"test-workflow-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == "done"
