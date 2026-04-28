"""End-to-end tests for LangGraph Functional API integration (v1 and v2).

Requires a running Temporal test server (started by conftest.py).
LangGraph's Functional API requires Python >= 3.11 for async context
variable propagation (see langgraph.config.get_config).
"""

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
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.func import (  # pyright: ignore[reportMissingTypeStubs]
    entrypoint as lg_entrypoint,
)
from langgraph.func import task  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import Command
from pytest import raises

from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import LangGraphPlugin, entrypoint
from temporalio.worker import Worker
from tests.contrib.langgraph.e2e_functional_entrypoints import (
    add_ten,
    ask_human,
    continue_as_new_entrypoint,
    double_value,
    expensive_task_a,
    expensive_task_b,
    expensive_task_c,
    get_task_execution_counts,
    interrupt_entrypoint,
    partial_execution_entrypoint,
    reset_task_execution_counts,
    simple_functional_entrypoint,
    slow_entrypoint,
    slow_task,
    step_1,
    step_2,
    step_3,
    step_4,
    step_5,
)
from tests.contrib.langgraph.e2e_functional_workflows import (
    ContinueAsNewFunctionalWorkflow,
    ContinueAsNewInput,
    PartialExecutionInput,
    PartialExecutionWorkflow,
    SimpleFunctionalE2EWorkflow,
    SlowFunctionalWorkflow,
)

_DEFAULT_ACTIVITY_OPTIONS = {"start_to_close_timeout": timedelta(seconds=30)}


def _execute_in_activity(*task_names: str) -> dict[str, dict[str, Any]]:
    return {name: {"execute_in": "activity"} for name in task_names}


# V2-only tasks defined here to avoid sharing mutated _TaskFunction objects
# (Plugin wraps task.func in-place).


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


@workflow.defn
class SimpleV2Workflow:
    def __init__(self) -> None:
        self.app = entrypoint("v2_simple")

    @workflow.run
    async def run(self, input_value: int) -> dict[str, Any]:
        result = await self.app.ainvoke(input_value, version="v2")
        return result.value


@workflow.defn
class InterruptV2FunctionalWorkflow:
    def __init__(self) -> None:
        self.app = entrypoint("v2_interrupt")
        self.app.checkpointer = InMemorySaver()

    @workflow.run
    async def run(self, input_value: str) -> dict[str, Any]:
        config = RunnableConfig(
            {"configurable": {"thread_id": workflow.info().workflow_id}}
        )

        result = await self.app.ainvoke(input_value, config, version="v2")

        assert result.value == {}
        assert len(result.interrupts) == 1
        assert result.interrupts[0].value == "Do you approve?"

        resumed = await self.app.ainvoke(
            Command(resume="approved"), config, version="v2"
        )
        return resumed.value


class TestFunctionalAPIBasicExecution:
    @pytest.mark.parametrize(
        "workflow_cls,entrypoint_func,entrypoint_name,tasks,expected_result",
        [
            (
                SimpleFunctionalE2EWorkflow,
                simple_functional_entrypoint,
                "e2e_simple_functional",
                [double_value, add_ten],
                30,
            ),
            (
                SimpleV2Workflow,
                simple_v2_entrypoint,
                "v2_simple",
                [triple_value, add_five],
                35,
            ),
        ],
        ids=["v1", "v2"],
    )
    async def test_simple_entrypoint(
        self,
        client: Client,
        workflow_cls: Any,
        entrypoint_func: Any,
        entrypoint_name: str,
        tasks: list,
        expected_result: int,
    ) -> None:
        task_queue = f"e2e-functional-{uuid4()}"

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[workflow_cls],
            plugins=[
                LangGraphPlugin(
                    entrypoints={entrypoint_name: entrypoint_func},
                    tasks=tasks,
                    activity_options=_execute_in_activity(
                        *(t.func.__name__ for t in tasks)
                    ),
                    default_activity_options=_DEFAULT_ACTIVITY_OPTIONS,
                )
            ],
        ):
            result = await client.execute_workflow(
                workflow_cls.run,
                10,
                id=f"e2e-functional-{uuid4()}",
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=30),
            )

        assert result["result"] == expected_result


class TestFunctionalAPIContinueAsNew:
    async def test_continue_as_new_with_checkpoint(self, client: Client) -> None:
        """10 * 3 = 30 -> + 100 = 130 -> * 2 = 260. Each task executes once."""
        reset_task_execution_counts()

        tasks = [expensive_task_a, expensive_task_b, expensive_task_c]
        task_queue = f"e2e-continue-as-new-{uuid4()}"

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[ContinueAsNewFunctionalWorkflow],
            plugins=[
                LangGraphPlugin(
                    entrypoints={
                        "e2e_continue_as_new_functional": continue_as_new_entrypoint
                    },
                    tasks=tasks,
                    activity_options=_execute_in_activity(
                        *(getattr(t.func, "__name__") for t in tasks)
                    ),
                    default_activity_options=_DEFAULT_ACTIVITY_OPTIONS,
                )
            ],
        ):
            result = await client.execute_workflow(
                ContinueAsNewFunctionalWorkflow.run,
                ContinueAsNewInput(value=10),
                id=f"e2e-continue-as-new-{uuid4()}",
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=60),
            )

        assert result["result"] == 260

        counts = get_task_execution_counts()
        assert (
            counts.get("task_a", 0) == 1
        ), f"task_a executed {counts.get('task_a', 0)} times, expected 1"
        assert (
            counts.get("task_b", 0) == 1
        ), f"task_b executed {counts.get('task_b', 0)} times, expected 1"
        assert (
            counts.get("task_c", 0) == 1
        ), f"task_c executed {counts.get('task_c', 0)} times, expected 1"


class TestFunctionalAPIPartialExecution:
    async def test_partial_execution_five_tasks(self, client: Client) -> None:
        """10*2=20 -> +5=25 -> *3=75 -> -10=65 -> +100=165. Each task executes once."""
        reset_task_execution_counts()

        tasks = [step_1, step_2, step_3, step_4, step_5]
        task_queue = f"e2e-partial-{uuid4()}"

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[PartialExecutionWorkflow],
            plugins=[
                LangGraphPlugin(
                    entrypoints={"e2e_partial_execution": partial_execution_entrypoint},
                    tasks=tasks,
                    activity_options=_execute_in_activity(
                        *(getattr(t.func, "__name__") for t in tasks)
                    ),
                    default_activity_options=_DEFAULT_ACTIVITY_OPTIONS,
                )
            ],
        ):
            result = await client.execute_workflow(
                PartialExecutionWorkflow.run,
                PartialExecutionInput(value=10),
                id=f"e2e-partial-{uuid4()}",
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=60),
            )

        assert result["result"] == 165
        assert result["completed_tasks"] == 5

        counts = get_task_execution_counts()
        for i in range(1, 6):
            assert (
                counts.get(f"step_{i}", 0) == 1
            ), f"step_{i} executed {counts.get(f'step_{i}', 0)} times, expected 1"


class TestFunctionalAPIInterruptV2:
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
                    activity_options=_execute_in_activity(
                        *(getattr(t.func, "__name__") for t in tasks)
                    ),
                    default_activity_options=_DEFAULT_ACTIVITY_OPTIONS,
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


class TestFunctionalAPIPerTaskOptions:
    async def test_per_task_activity_options_override(self, client: Client) -> None:
        """activity_options[task_name] overrides default_activity_options for that task."""
        task_queue = f"e2e-per-task-options-{uuid4()}"

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SlowFunctionalWorkflow],
            plugins=[
                LangGraphPlugin(
                    entrypoints={"e2e_slow_functional": slow_entrypoint},
                    tasks=[slow_task],
                    default_activity_options=_DEFAULT_ACTIVITY_OPTIONS,
                    activity_options={
                        "slow_task": {
                            "execute_in": "activity",
                            "start_to_close_timeout": timedelta(milliseconds=100),
                            "retry_policy": RetryPolicy(maximum_attempts=1),
                        }
                    },
                )
            ],
        ):
            with raises(WorkflowFailureError):
                await client.execute_workflow(
                    SlowFunctionalWorkflow.run,
                    1,
                    id=f"e2e-per-task-options-{uuid4()}",
                    task_queue=task_queue,
                    execution_timeout=timedelta(seconds=30),
                )
