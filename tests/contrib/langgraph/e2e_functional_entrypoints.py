"""Functional API entrypoint definitions for E2E tests.

These define @task and @entrypoint functions used in functional API E2E tests.
"""

from __future__ import annotations

from langgraph.func import entrypoint, task


@task
def double_value(x: int) -> int:
    """Simple task that doubles a value."""
    return x * 2


@task
def add_ten(x: int) -> int:
    """Simple task that adds 10."""
    return x + 10


@entrypoint()
async def simple_functional_entrypoint(value: int) -> dict:
    """Simple entrypoint that calls tasks and returns result.

    This entrypoint:
    1. Doubles the input value via a task
    2. Adds 10 via another task
    3. Returns the result
    """
    doubled = await double_value(value)
    result = await add_ten(doubled)
    return {"result": result}


# Track task execution count for continue-as-new testing
_task_execution_counts: dict[str, int] = {}


def get_task_execution_counts() -> dict[str, int]:
    """Get the current task execution counts (for testing)."""
    return _task_execution_counts.copy()


def reset_task_execution_counts() -> None:
    """Reset task execution counts (for testing)."""
    _task_execution_counts.clear()


@task
def expensive_task_a(x: int) -> int:
    """A task that tracks its execution count."""
    _task_execution_counts["task_a"] = _task_execution_counts.get("task_a", 0) + 1
    return x * 3


@task
def expensive_task_b(x: int) -> int:
    """A task that tracks its execution count."""
    _task_execution_counts["task_b"] = _task_execution_counts.get("task_b", 0) + 1
    return x + 100


@task
def expensive_task_c(x: int) -> int:
    """A task that tracks its execution count."""
    _task_execution_counts["task_c"] = _task_execution_counts.get("task_c", 0) + 1
    return x * 2


@entrypoint()
async def continue_as_new_entrypoint(value: int) -> dict:
    """Entrypoint for testing continue-as-new with task caching.

    Executes three tasks in sequence:
    1. task_a: x * 3
    2. task_b: x + 100
    3. task_c: x * 2

    For input 10: 10 * 3 = 30 -> 30 + 100 = 130 -> 130 * 2 = 260
    """
    result_a = await expensive_task_a(value)
    result_b = await expensive_task_b(result_a)
    result_c = await expensive_task_c(result_b)
    return {"result": result_c}


# ============================================================================
# Partial Execution Test (5 tasks, run 3 then 2)
# ============================================================================


@task
def step_1(x: int) -> int:
    """Step 1: multiply by 2."""
    _task_execution_counts["step_1"] = _task_execution_counts.get("step_1", 0) + 1
    return x * 2


@task
def step_2(x: int) -> int:
    """Step 2: add 5."""
    _task_execution_counts["step_2"] = _task_execution_counts.get("step_2", 0) + 1
    return x + 5


@task
def step_3(x: int) -> int:
    """Step 3: multiply by 3."""
    _task_execution_counts["step_3"] = _task_execution_counts.get("step_3", 0) + 1
    return x * 3


@task
def step_4(x: int) -> int:
    """Step 4: subtract 10."""
    _task_execution_counts["step_4"] = _task_execution_counts.get("step_4", 0) + 1
    return x - 10


@task
def step_5(x: int) -> int:
    """Step 5: add 100."""
    _task_execution_counts["step_5"] = _task_execution_counts.get("step_5", 0) + 1
    return x + 100


@entrypoint()
async def partial_execution_entrypoint(input_data: dict) -> dict:
    """Entrypoint for testing partial execution with continue-as-new.

    Accepts a dict with:
    - value: int - the starting value
    - stop_after: int - stop after this many tasks (1-5)

    This allows testing scenarios where:
    - First run: stop_after=3 -> runs tasks 1-3
    - Second run: stop_after=5 -> runs all 5 (but 1-3 are cached)

    Calculation for value=10, all 5 tasks:
    10 * 2 = 20 -> 20 + 5 = 25 -> 25 * 3 = 75 -> 75 - 10 = 65 -> 65 + 100 = 165
    """
    value = input_data["value"]
    stop_after = input_data.get("stop_after", 5)

    result = value

    # Task 1
    result = await step_1(result)
    if stop_after == 1:
        return {"result": result, "completed_tasks": 1}

    # Task 2
    result = await step_2(result)
    if stop_after == 2:
        return {"result": result, "completed_tasks": 2}

    # Task 3
    result = await step_3(result)
    if stop_after == 3:
        return {"result": result, "completed_tasks": 3}

    # Task 4
    result = await step_4(result)
    if stop_after == 4:
        return {"result": result, "completed_tasks": 4}

    # Task 5
    result = await step_5(result)
    return {"result": result, "completed_tasks": 5}
