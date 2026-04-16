"""Tests for LangGraph Functional API with version="v2".

version="v2" changes ainvoke() to return a GraphOutput dataclass with
.value and .interrupts fields instead of a plain dict with __interrupt__
mixed in.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.func import (  # pyright: ignore[reportMissingTypeStubs]
    entrypoint as lg_entrypoint,
)
from langgraph.func import task  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Command

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin, entrypoint
from temporalio.worker import Worker
from tests.contrib.langgraph.e2e_functional_entrypoints import (
    ask_human,
    interrupt_entrypoint,
)

# Define separate tasks to avoid sharing mutated _TaskFunction objects with
# other tests (Plugin wraps task.func in-place).


@task
def triple_value(x: int) -> int:
    return x * 3


@task
def add_five(x: int) -> int:
    return x + 5


@lg_entrypoint()
async def simple_v2_entrypoint(value: int) -> dict:
    tripled = await triple_value(value)
    result = await add_five(tripled)
    return {"result": result}


# -- Workflows ----------------------------------------------------------------


@workflow.defn
class SimpleV2Workflow:
    @workflow.run
    async def run(self, input_value: int) -> dict[str, Any]:
        result = await entrypoint("v2_simple").ainvoke(input_value, version="v2")
        # v2 returns GraphOutput — extract .value for Temporal serialization
        return result.value


@workflow.defn
class InterruptV2FunctionalWorkflow:
    @workflow.run
    async def run(self, input_value: str) -> dict[str, Any]:
        app = entrypoint("v2_interrupt")
        app.checkpointer = InMemorySaver()
        config = RunnableConfig(
            {"configurable": {"thread_id": workflow.info().workflow_id}}
        )

        # First invoke — should get an interrupt
        result = await app.ainvoke(input_value, config, version="v2")

        # v2: interrupts are on result.interrupts, value is clean
        assert result.value == {}
        assert len(result.interrupts) == 1
        assert result.interrupts[0].value == "Do you approve?"

        # Resume with approval
        resumed = await app.ainvoke(Command(resume="approved"), config, version="v2")
        return resumed.value


# -- Tests --------------------------------------------------------------------


class TestFunctionalAPIV2:
    async def test_simple_v2(self, client: Client) -> None:
        """version='v2' returns GraphOutput with .value containing the result."""
        tasks = [triple_value, add_five]
        task_queue = f"v2-simple-{uuid4()}"

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SimpleV2Workflow],
            plugins=[
                LangGraphPlugin(
                    entrypoints={"v2_simple": simple_v2_entrypoint},
                    tasks=tasks,
                    default_activity_options={
                        "start_to_close_timeout": timedelta(seconds=30)
                    },
                )
            ],
        ):
            result = await client.execute_workflow(
                SimpleV2Workflow.run,
                10,
                id=f"v2-simple-{uuid4()}",
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=30),
            )

        # 10 * 3 = 30, 30 + 5 = 35
        assert result["result"] == 35

    async def test_interrupt_v2_functional(self, client: Client) -> None:
        """version='v2' separates interrupts from value in functional API."""
        tasks = [ask_human]
        task_queue = f"v2-interrupt-{uuid4()}"

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[InterruptV2FunctionalWorkflow],
            plugins=[
                LangGraphPlugin(
                    entrypoints={"v2_interrupt": interrupt_entrypoint},
                    tasks=tasks,
                    default_activity_options={
                        "start_to_close_timeout": timedelta(seconds=30)
                    },
                )
            ],
        ):
            result = await client.execute_workflow(
                InterruptV2FunctionalWorkflow.run,
                "hello",
                id=f"v2-interrupt-{uuid4()}",
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=30),
            )

        assert result["input"] == "hello"
        assert result["answer"] == "approved"
