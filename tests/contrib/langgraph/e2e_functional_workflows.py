"""Workflow definitions for Functional API E2E tests.

Workflow classes that use compile to run @entrypoint functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.contrib.langgraph import compile


@workflow.defn
class SimpleFunctionalE2EWorkflow:
    """Simple workflow using functional API.

    Compiles and runs the simple_functional_entrypoint.
    """

    @workflow.run
    async def run(self, input_value: int) -> dict:
        app = compile("e2e_simple_functional")
        return await app.ainvoke(input_value)


@dataclass
class ContinueAsNewInput:
    """Input for ContinueAsNewFunctionalWorkflow."""

    value: int
    checkpoint: dict[str, Any] | None = None
    task_a_done: bool = False
    task_b_done: bool = False


@workflow.defn
class ContinueAsNewFunctionalWorkflow:
    """Workflow demonstrating continue-as-new with checkpoint for Functional API.

    This workflow demonstrates the checkpoint pattern:
    1. Execute some tasks
    2. After task_a completes, continue-as-new with checkpoint
    3. After task_b completes, continue-as-new with checkpoint
    4. Final execution completes task_c and returns result

    This verifies that cached task results carry over via checkpoint.
    """

    @workflow.run
    async def run(self, input_data: ContinueAsNewInput) -> dict[str, Any]:
        # Compile with checkpoint if provided (from previous continue-as-new)
        app = compile(
            "e2e_continue_as_new_functional", checkpoint=input_data.checkpoint
        )

        # Run the entrypoint
        result = await app.ainvoke(input_data.value)

        # Get checkpoint state for continue-as-new
        # Cast to dict since TemporalFunctionalRunner.get_state() returns dict
        checkpoint: dict[str, Any] = app.get_state()  # type: ignore[assignment]

        # Simulate continue-as-new after each phase
        if not input_data.task_a_done:
            # First execution - task_a completed, continue-as-new
            workflow.continue_as_new(
                ContinueAsNewInput(
                    value=input_data.value,
                    checkpoint=checkpoint,
                    task_a_done=True,
                    task_b_done=False,
                )
            )

        if not input_data.task_b_done:
            # Second execution - task_b completed, continue-as-new
            workflow.continue_as_new(
                ContinueAsNewInput(
                    value=input_data.value,
                    checkpoint=checkpoint,
                    task_a_done=True,
                    task_b_done=True,
                )
            )

        # Third execution - all tasks done, return result
        return result


@dataclass
class PartialExecutionInput:
    """Input for PartialExecutionWorkflow."""

    value: int
    checkpoint: dict[str, Any] | None = None
    phase: int = 1  # 1 = first run (3 tasks), 2 = second run (all 5)


@workflow.defn
class PartialExecutionWorkflow:
    """Workflow demonstrating partial execution with continue-as-new.

    This workflow:
    1. First run (phase=1): executes 3 tasks via stop_after=3, then continues-as-new
    2. Second run (phase=2): executes all 5 tasks, but tasks 1-3 are cached

    This verifies that:
    - Tasks 1-3 execute only in the first run
    - Tasks 4-5 execute only in the second run
    - The final result is correct (all 5 tasks applied)
    """

    @workflow.run
    async def run(self, input_data: PartialExecutionInput) -> dict[str, Any]:
        # Compile with checkpoint if provided (from previous continue-as-new)
        app = compile("e2e_partial_execution", checkpoint=input_data.checkpoint)

        if input_data.phase == 1:
            # First run: execute only first 3 tasks
            result = await app.ainvoke(
                {
                    "value": input_data.value,
                    "stop_after": 3,
                }
            )

            # Get checkpoint with cached task results
            checkpoint: dict[str, Any] = app.get_state()  # type: ignore[assignment]

            # Continue-as-new for phase 2
            workflow.continue_as_new(
                PartialExecutionInput(
                    value=input_data.value,
                    checkpoint=checkpoint,
                    phase=2,
                )
            )

        # Phase 2: execute all 5 tasks (but 1-3 are cached)
        result = await app.ainvoke(
            {
                "value": input_data.value,
                "stop_after": 5,
            }
        )

        return result
