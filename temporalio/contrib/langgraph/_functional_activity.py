"""Temporal activity for executing LangGraph functional API tasks.

This module provides the dynamic activity that executes @task-decorated functions
by their module path. Tasks are resolved at runtime via importlib.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Any

from temporalio import activity
from temporalio.contrib.langgraph._functional_models import (
    TaskActivityInput,
    TaskActivityOutput,
)
from temporalio.exceptions import ApplicationError

logger = logging.getLogger(__name__)


def _resolve_task_function(task_id: str) -> Any:
    """Resolve a task function from its module path.

    Args:
        task_id: Full module path like 'mymodule.tasks.research_topic'

    Returns:
        The callable task function.

    Raises:
        ApplicationError: If the task cannot be imported or found.
    """
    try:
        # Split into module and function name
        module_name, func_name = task_id.rsplit(".", 1)
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)
        return func
    except (ValueError, ImportError, AttributeError) as e:
        raise ApplicationError(
            f"Cannot import task '{task_id}': {e}",
            type="TASK_NOT_FOUND",
            non_retryable=True,
        ) from e


def _unwrap_task_function(func: Any) -> Any:
    """Unwrap a @task-decorated function to get the underlying callable.

    LangGraph's @task decorator wraps functions in a _TaskFunction class.
    We need to get the original function to execute it.
    """
    # Check for LangGraph's _TaskFunction wrapper
    if hasattr(func, "func"):
        return func.func

    # Check for RunnableCallable wrapper
    if hasattr(func, "afunc") and func.afunc is not None:
        return func.afunc
    if hasattr(func, "func_"):
        return func.func_

    return func


@activity.defn(name="execute_langgraph_task")
async def execute_langgraph_task(input: TaskActivityInput) -> TaskActivityOutput:
    """Execute a LangGraph @task function by its module path.

    This is a dynamic activity that can execute any @task-decorated function.
    The task is resolved at runtime via importlib using the task_id.

    Args:
        input: Task execution input with task_id and arguments.

    Returns:
        TaskActivityOutput with the result or error.
    """
    logger.debug(
        "Executing task %s with args=%s, kwargs=%s",
        input.task_id,
        input.args,
        input.kwargs,
    )

    try:
        # Resolve the task function
        func = _resolve_task_function(input.task_id)

        # Unwrap any decorators to get the actual callable
        actual_func = _unwrap_task_function(func)

        # Execute the task
        if asyncio.iscoroutinefunction(actual_func):
            result = await actual_func(*input.args, **input.kwargs)
        else:
            # Run sync function in executor to not block
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: actual_func(*input.args, **input.kwargs)
            )

        logger.debug(
            "Task %s completed with result type: %s", input.task_id, type(result)
        )
        return TaskActivityOutput(result=result)

    except ApplicationError:
        # Re-raise ApplicationError as-is
        raise
    except Exception as e:
        logger.exception("Task %s failed with error: %s", input.task_id, e)
        # Check if this is a non-retryable error type
        error_type = type(e).__name__

        # LangGraph-specific non-retryable errors
        non_retryable_types = {
            "GraphRecursionError",
            "InvalidUpdateError",
            "EmptyInputError",
        }

        if error_type in non_retryable_types:
            raise ApplicationError(
                f"Task failed: {e}",
                type=error_type,
                non_retryable=True,
            ) from e

        # Other errors - let Temporal retry
        raise
