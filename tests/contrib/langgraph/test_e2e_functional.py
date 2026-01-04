"""End-to-end tests for LangGraph Functional API integration.

These tests verify the complete functional API integration with Temporal.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin, activity_options
from tests.contrib.langgraph.e2e_functional_entrypoints import (
    continue_as_new_entrypoint,
    get_task_execution_counts,
    partial_execution_entrypoint,
    reset_task_execution_counts,
    simple_functional_entrypoint,
)
from tests.contrib.langgraph.e2e_functional_workflows import (
    ContinueAsNewFunctionalWorkflow,
    ContinueAsNewInput,
    PartialExecutionInput,
    PartialExecutionWorkflow,
    SimpleFunctionalE2EWorkflow,
)
from tests.helpers import new_worker


class TestFunctionalAPIBasicExecution:
    """Tests for basic functional API execution."""

    @pytest.mark.asyncio
    async def test_simple_functional_entrypoint(self, client: Client) -> None:
        """Test basic functional API entrypoint execution.

        This test verifies that:
        1. The unified plugin correctly registers entrypoints
        2. compile works inside a workflow
        3. @task functions are executed as activities
        4. The result is returned correctly

        Expected: input 10 -> double (20) -> add 10 (30) -> result: 30
        """
        plugin = LangGraphPlugin(
            graphs={"e2e_simple_functional": simple_functional_entrypoint},
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(seconds=30)
            ),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, SimpleFunctionalE2EWorkflow) as worker:
            result = await plugin_client.execute_workflow(
                SimpleFunctionalE2EWorkflow.run,
                10,
                id=f"e2e-functional-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            # 10 * 2 = 20, 20 + 10 = 30
            assert result["result"] == 30


class TestFunctionalAPIContinueAsNew:
    """Tests for continue-as-new with task caching."""

    @pytest.mark.asyncio
    async def test_continue_as_new_with_checkpoint(self, client: Client) -> None:
        """Test continue-as-new preserves cached task results.

        This test verifies that:
        1. Task results are cached in the runner
        2. Checkpoint captures the cache state
        3. Continue-as-new passes checkpoint to new execution
        4. New execution uses cached results instead of re-executing tasks

        The workflow continues-as-new twice (after task_a, after task_b).
        Without caching, each task would execute 3 times (once per execution).
        With caching, each task should only execute once.

        Expected result: 10 * 3 = 30 -> 30 + 100 = 130 -> 130 * 2 = 260
        """
        # Reset execution counters
        reset_task_execution_counts()

        plugin = LangGraphPlugin(
            graphs={"e2e_continue_as_new_functional": continue_as_new_entrypoint},
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(seconds=30)
            ),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(
            plugin_client, ContinueAsNewFunctionalWorkflow
        ) as worker:
            result = await plugin_client.execute_workflow(
                ContinueAsNewFunctionalWorkflow.run,
                ContinueAsNewInput(value=10),
                id=f"e2e-functional-continue-as-new-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )

            # Verify correct result: 10 * 3 = 30 -> 30 + 100 = 130 -> 130 * 2 = 260
            assert result["result"] == 260

            # Verify each task was only executed once (cached for continue-as-new)
            counts = get_task_execution_counts()
            assert counts.get("task_a", 0) == 1, f"task_a executed {counts.get('task_a', 0)} times, expected 1"
            assert counts.get("task_b", 0) == 1, f"task_b executed {counts.get('task_b', 0)} times, expected 1"
            assert counts.get("task_c", 0) == 1, f"task_c executed {counts.get('task_c', 0)} times, expected 1"


class TestFunctionalAPIPartialExecution:
    """Tests for partial execution with continue-as-new (5 tasks: 3 then 2)."""

    @pytest.mark.asyncio
    async def test_partial_execution_five_tasks(self, client: Client) -> None:
        """Test partial execution: 3 tasks in first run, 2 in second run.

        This test verifies that:
        1. First run executes tasks 1-3 only (stop_after=3)
        2. Continue-as-new passes checkpoint with cached results
        3. Second run executes all 5 tasks, but tasks 1-3 use cached results
        4. Only tasks 4-5 actually execute in the second run
        5. Each task executes exactly once across both runs

        Calculation for value=10:
        - step_1: 10 * 2 = 20
        - step_2: 20 + 5 = 25
        - step_3: 25 * 3 = 75
        - step_4: 75 - 10 = 65
        - step_5: 65 + 100 = 165
        """
        # Reset execution counters
        reset_task_execution_counts()

        plugin = LangGraphPlugin(
            graphs={"e2e_partial_execution": partial_execution_entrypoint},
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(seconds=30)
            ),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, PartialExecutionWorkflow) as worker:
            result = await plugin_client.execute_workflow(
                PartialExecutionWorkflow.run,
                PartialExecutionInput(value=10),
                id=f"e2e-functional-partial-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )

            # Verify correct final result
            # 10 * 2 = 20 -> 20 + 5 = 25 -> 25 * 3 = 75 -> 75 - 10 = 65 -> 65 + 100 = 165
            assert result["result"] == 165
            assert result["completed_tasks"] == 5

            # Verify each task was executed exactly once
            counts = get_task_execution_counts()
            assert counts.get("step_1", 0) == 1, f"step_1 executed {counts.get('step_1', 0)} times, expected 1"
            assert counts.get("step_2", 0) == 1, f"step_2 executed {counts.get('step_2', 0)} times, expected 1"
            assert counts.get("step_3", 0) == 1, f"step_3 executed {counts.get('step_3', 0)} times, expected 1"
            assert counts.get("step_4", 0) == 1, f"step_4 executed {counts.get('step_4', 0)} times, expected 1"
            assert counts.get("step_5", 0) == 1, f"step_5 executed {counts.get('step_5', 0)} times, expected 1"
