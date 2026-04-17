"""Workflow definitions for Functional API E2E tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import workflow
from temporalio.contrib.langgraph.langgraph_plugin import get_cache, set_cache
from tests.contrib.langgraph.e2e_functional_entrypoints import (
    continue_as_new_entrypoint,
    partial_execution_entrypoint,
    simple_functional_entrypoint,
    slow_entrypoint,
)


@workflow.defn
class SimpleFunctionalE2EWorkflow:
    @workflow.run
    async def run(self, input_value: int) -> dict:
        return await simple_functional_entrypoint.ainvoke(input_value)


@workflow.defn
class SlowFunctionalWorkflow:
    @workflow.run
    async def run(self, input_value: int) -> dict:
        return await slow_entrypoint.ainvoke(input_value)


@dataclass
class ContinueAsNewInput:
    value: int
    cache: dict[str, Any] | None = None
    task_a_done: bool = False
    task_b_done: bool = False


@workflow.defn
class ContinueAsNewFunctionalWorkflow:
    """Continues-as-new after each phase, passing cache for task deduplication."""

    @workflow.run
    async def run(self, input_data: ContinueAsNewInput) -> dict[str, Any]:
        set_cache(input_data.cache)

        result = await continue_as_new_entrypoint.ainvoke(input_data.value)

        if not input_data.task_a_done:
            workflow.continue_as_new(
                ContinueAsNewInput(
                    value=input_data.value,
                    cache=get_cache(),
                    task_a_done=True,
                )
            )

        if not input_data.task_b_done:
            workflow.continue_as_new(
                ContinueAsNewInput(
                    value=input_data.value,
                    cache=get_cache(),
                    task_a_done=True,
                    task_b_done=True,
                )
            )

        return result


@dataclass
class PartialExecutionInput:
    value: int
    cache: dict[str, Any] | None = None
    phase: int = 1


@workflow.defn
class PartialExecutionWorkflow:
    """Phase 1: 3 tasks + cache. Phase 2: all 5 (1-3 cached)."""

    @workflow.run
    async def run(self, input_data: PartialExecutionInput) -> dict[str, Any]:
        set_cache(input_data.cache)

        if input_data.phase == 1:
            await partial_execution_entrypoint.ainvoke(
                {"value": input_data.value, "stop_after": 3}
            )
            workflow.continue_as_new(
                PartialExecutionInput(
                    value=input_data.value,
                    cache=get_cache(),
                    phase=2,
                )
            )

        return await partial_execution_entrypoint.ainvoke(
            {"value": input_data.value, "stop_after": 5}
        )
