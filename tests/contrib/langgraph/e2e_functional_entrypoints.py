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
