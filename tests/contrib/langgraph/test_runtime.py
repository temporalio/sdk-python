from datetime import timedelta
from typing import Any
from uuid import uuid4

from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.runtime import Runtime
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, graph
from temporalio.worker import Worker


class Context(TypedDict):
    user_id: str


class State(TypedDict):
    user_id: str


async def read_user_id(state: State, runtime: Runtime[Context]) -> dict[str, str]:
    return {"user_id": runtime.context["user_id"]}


@workflow.defn
class RuntimeContextWorkflow:
    def __init__(self) -> None:
        self.app = graph("my-graph").compile()

    @workflow.run
    async def run(self, user_id: str) -> Any:
        return await self.app.ainvoke(
            {"user_id": ""}, context=Context(user_id=user_id)
        )


async def test_runtime_context(client: Client):
    g = StateGraph(State, context_schema=Context)
    g.add_node("read_user_id", read_user_id, metadata={"execute_in": "activity"})
    g.add_edge(START, "read_user_id")

    task_queue = f"runtime-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[RuntimeContextWorkflow],
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
            RuntimeContextWorkflow.run,
            "user-123",
            id=f"test-runtime-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"user_id": "user-123"}
