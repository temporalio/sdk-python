"""End-to-end tests for LangGraph Functional API integration.

Requires a running Temporal test server (started by conftest.py).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from temporalio.client import Client
from temporalio.worker import Worker

from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin
from tests.contrib.langgraph.e2e_functional_entrypoints import (
    add_ten,
    continue_as_new_entrypoint,
    double_value,
    expensive_task_a,
    expensive_task_b,
    expensive_task_c,
    get_task_execution_counts,
    partial_execution_entrypoint,
    reset_task_execution_counts,
    simple_functional_entrypoint,
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
)


def _activity_opts(*task_funcs) -> dict[str, dict]:
    """Build activity_options dict giving every task the same 30s timeout."""
    return {
        t.func.__name__: {"start_to_close_timeout": timedelta(seconds=30)}
        for t in task_funcs
    }


class TestFunctionalAPIBasicExecution:
    async def test_simple_functional_entrypoint(self, client: Client) -> None:
        """input 10 -> double (20) -> add 10 (30) -> result: 30"""
        tasks = [double_value, add_ten]
        task_queue = f"e2e-functional-{uuid4()}"

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SimpleFunctionalE2EWorkflow],
            plugins=[
                LangGraphPlugin(
                    entrypoints={"e2e_simple_functional": simple_functional_entrypoint},
                    tasks=tasks,
                    activity_options=_activity_opts(*tasks),
                )
            ],
        ):
            result = await client.execute_workflow(
                SimpleFunctionalE2EWorkflow.run,
                10,
                id=f"e2e-functional-{uuid4()}",
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=30),
            )

        assert result["result"] == 30


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
                    activity_options=_activity_opts(*tasks),
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
        assert counts.get("task_a", 0) == 1, (
            f"task_a executed {counts.get('task_a', 0)} times, expected 1"
        )
        assert counts.get("task_b", 0) == 1, (
            f"task_b executed {counts.get('task_b', 0)} times, expected 1"
        )
        assert counts.get("task_c", 0) == 1, (
            f"task_c executed {counts.get('task_c', 0)} times, expected 1"
        )


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
                    activity_options=_activity_opts(*tasks),
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
            assert counts.get(f"step_{i}", 0) == 1, (
                f"step_{i} executed {counts.get(f'step_{i}', 0)} times, expected 1"
            )
