from asyncio import sleep
from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from pytest import raises
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin, graph
from temporalio.worker import Worker


class State(TypedDict):
    value: str


async def node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    await sleep(1)  # 1 second
    return {"value": "done"}


@workflow.defn
class TimeoutWorkflow:
    @workflow.run
    async def run(self, input: str) -> Any:
        return await graph("my-graph").compile().ainvoke({"value": input})


async def test_timeout(client: Client):
    g = StateGraph(State)
    g.add_node(
        "node",
        node,
        metadata={
            "start_to_close_timeout": timedelta(milliseconds=100),
            "retry_policy": RetryPolicy(maximum_attempts=1),
        },
    )
    g.add_edge(START, "node")

    task_queue = f"my-graph-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[TimeoutWorkflow],
        plugins=[LangGraphPlugin(graphs={"my-graph": g})],
    ):
        with raises(WorkflowFailureError):
            await client.execute_workflow(
                TimeoutWorkflow.run,
                "",
                id=f"test-workflow-{uuid4()}",
                task_queue=task_queue,
            )
