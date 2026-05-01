from __future__ import annotations

import sys
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="LangGraph Functional API requires Python >= 3.11 for async context propagation",
)

from langgraph.func import (  # pyright: ignore[reportMissingTypeStubs]
    entrypoint as lg_entrypoint,
)
from langgraph.func import task  # pyright: ignore[reportMissingTypeStubs]
from langgraph.runtime import get_runtime
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, entrypoint
from temporalio.worker import Worker


class Context(TypedDict):
    user_id: str


@task
async def read_user_id() -> str:
    runtime = get_runtime(Context)
    return runtime.context["user_id"]


@lg_entrypoint()
async def read_user_id_entrypoint(_: str) -> dict[str, str]:
    user_id = await read_user_id()
    return {"user_id": user_id}


@workflow.defn
class FunctionalRuntimeContextWorkflow:
    def __init__(self) -> None:
        self.app = entrypoint("read_user_id")

    @workflow.run
    async def run(self, user_id: str) -> Any:
        return await self.app.ainvoke("", context=Context(user_id=user_id))


async def test_functional_runtime_context(client: Client) -> None:
    task_queue = f"functional-runtime-{uuid4()}"

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[FunctionalRuntimeContextWorkflow],
        plugins=[
            LangGraphPlugin(
                entrypoints={"read_user_id": read_user_id_entrypoint},
                tasks=[read_user_id],
                activity_options={"read_user_id": {"execute_in": "activity"}},
                default_activity_options={
                    "start_to_close_timeout": timedelta(seconds=10)
                },
            )
        ],
    ):
        result = await client.execute_workflow(
            FunctionalRuntimeContextWorkflow.run,
            "user-123",
            id=f"test-functional-runtime-{uuid4()}",
            task_queue=task_queue,
        )

    assert result == {"user_id": "user-123"}
