"""Functional API entrypoint definitions for E2E tests.

These define @task and @entrypoint functions used in functional API E2E tests.
"""

from __future__ import annotations

import asyncio

import langgraph.types
from langgraph.func import entrypoint, task  # pyright: ignore[reportMissingTypeStubs]


@task
def double_value(x: int) -> int:
    return x * 2


@task
def add_ten(x: int) -> int:
    return x + 10


@entrypoint()
async def simple_functional_entrypoint(value: int) -> dict:
    doubled = await double_value(value)
    result = await add_ten(doubled)
    return {"result": result}


# Track task execution count for continue-as-new testing
_task_execution_counts: dict[str, int] = {}


def get_task_execution_counts() -> dict[str, int]:
    return _task_execution_counts.copy()


def reset_task_execution_counts() -> None:
    _task_execution_counts.clear()


@task
def expensive_task_a(x: int) -> int:
    _task_execution_counts["task_a"] = _task_execution_counts.get("task_a", 0) + 1
    return x * 3


@task
def expensive_task_b(x: int) -> int:
    _task_execution_counts["task_b"] = _task_execution_counts.get("task_b", 0) + 1
    return x + 100


@task
def expensive_task_c(x: int) -> int:
    _task_execution_counts["task_c"] = _task_execution_counts.get("task_c", 0) + 1
    return x * 2


@entrypoint()
async def continue_as_new_entrypoint(value: int) -> dict:
    """For input 10: 10 * 3 = 30 -> 30 + 100 = 130 -> 130 * 2 = 260"""
    result_a = await expensive_task_a(value)
    result_b = await expensive_task_b(result_a)
    result_c = await expensive_task_c(result_b)
    return {"result": result_c}


@task
def step_1(x: int) -> int:
    _task_execution_counts["step_1"] = _task_execution_counts.get("step_1", 0) + 1
    return x * 2


@task
def step_2(x: int) -> int:
    _task_execution_counts["step_2"] = _task_execution_counts.get("step_2", 0) + 1
    return x + 5


@task
def step_3(x: int) -> int:
    _task_execution_counts["step_3"] = _task_execution_counts.get("step_3", 0) + 1
    return x * 3


@task
def step_4(x: int) -> int:
    _task_execution_counts["step_4"] = _task_execution_counts.get("step_4", 0) + 1
    return x - 10


@task
def step_5(x: int) -> int:
    _task_execution_counts["step_5"] = _task_execution_counts.get("step_5", 0) + 1
    return x + 100


@entrypoint()
async def partial_execution_entrypoint(input_data: dict) -> dict:
    """For value=10, all 5 tasks: 10*2=20 -> +5=25 -> *3=75 -> -10=65 -> +100=165"""
    value = input_data["value"]
    stop_after = input_data.get("stop_after", 5)

    result = value
    result = await step_1(result)
    if stop_after == 1:
        return {"result": result, "completed_tasks": 1}
    result = await step_2(result)
    if stop_after == 2:
        return {"result": result, "completed_tasks": 2}
    result = await step_3(result)
    if stop_after == 3:
        return {"result": result, "completed_tasks": 3}
    result = await step_4(result)
    if stop_after == 4:
        return {"result": result, "completed_tasks": 4}
    result = await step_5(result)
    return {"result": result, "completed_tasks": 5}


@task
def ask_human(question: str) -> str:
    return langgraph.types.interrupt(question)


@entrypoint()
async def interrupt_entrypoint(value: str) -> dict:
    """Entrypoint that interrupts for human input, then returns the answer."""
    answer = await ask_human("Do you approve?")
    return {"input": value, "answer": answer}


@task
async def slow_task(x: int) -> int:
    await asyncio.sleep(1)
    return x


@entrypoint()
async def slow_entrypoint(value: int) -> dict:
    result = await slow_task(value)
    return {"result": result}
