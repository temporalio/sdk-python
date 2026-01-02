"""Unit tests for LangGraph Functional API activity.

Tests for _resolve_task_function, _unwrap_task_function, and execute_langgraph_task.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from temporalio.exceptions import ApplicationError


class TestResolveTaskFunction:
    """Tests for _resolve_task_function."""

    def test_resolve_valid_function(self) -> None:
        """Should resolve a valid module.function path."""
        from temporalio.contrib.langgraph._functional_activity import (
            _resolve_task_function,
        )

        # Use a known built-in function
        result = _resolve_task_function("json.dumps")

        import json

        assert result is json.dumps

    def test_resolve_nested_module(self) -> None:
        """Should resolve function from nested module."""
        from temporalio.contrib.langgraph._functional_activity import (
            _resolve_task_function,
        )

        result = _resolve_task_function("os.path.join")

        import os.path

        assert result is os.path.join

    def test_resolve_invalid_module_raises_error(self) -> None:
        """Should raise ApplicationError for invalid module."""
        from temporalio.contrib.langgraph._functional_activity import (
            _resolve_task_function,
        )

        with pytest.raises(ApplicationError) as exc_info:
            _resolve_task_function("nonexistent_module_xyz.some_func")

        assert "Cannot import task" in str(exc_info.value)
        assert exc_info.value.type == "TASK_NOT_FOUND"
        assert exc_info.value.non_retryable is True

    def test_resolve_invalid_function_raises_error(self) -> None:
        """Should raise ApplicationError for invalid function name."""
        from temporalio.contrib.langgraph._functional_activity import (
            _resolve_task_function,
        )

        with pytest.raises(ApplicationError) as exc_info:
            _resolve_task_function("json.nonexistent_function_xyz")

        assert "Cannot import task" in str(exc_info.value)
        assert exc_info.value.type == "TASK_NOT_FOUND"

    def test_resolve_invalid_format_raises_error(self) -> None:
        """Should raise ApplicationError for invalid format (no dot)."""
        from temporalio.contrib.langgraph._functional_activity import (
            _resolve_task_function,
        )

        with pytest.raises(ApplicationError) as exc_info:
            _resolve_task_function("no_dot_in_name")

        assert "Cannot import task" in str(exc_info.value)


class TestUnwrapTaskFunction:
    """Tests for _unwrap_task_function."""

    def test_unwrap_plain_function(self) -> None:
        """Should return plain function unchanged."""
        from temporalio.contrib.langgraph._functional_activity import (
            _unwrap_task_function,
        )

        def my_func():
            return 42

        result = _unwrap_task_function(my_func)

        assert result is my_func

    def test_unwrap_task_function_wrapper(self) -> None:
        """Should unwrap LangGraph _TaskFunction wrapper via .func."""
        from temporalio.contrib.langgraph._functional_activity import (
            _unwrap_task_function,
        )

        def original_func():
            return 42

        # Mock LangGraph's _TaskFunction wrapper
        wrapper = MagicMock()
        wrapper.func = original_func

        result = _unwrap_task_function(wrapper)

        assert result is original_func

    def test_unwrap_runnable_callable_afunc(self) -> None:
        """Should unwrap RunnableCallable via .afunc."""
        from temporalio.contrib.langgraph._functional_activity import (
            _unwrap_task_function,
        )

        async def original_async_func():
            return 42

        # Mock RunnableCallable with afunc
        wrapper = MagicMock(spec=[])  # Empty spec so hasattr checks work properly
        wrapper.afunc = original_async_func

        result = _unwrap_task_function(wrapper)

        assert result is original_async_func

    def test_unwrap_runnable_callable_func_(self) -> None:
        """Should unwrap RunnableCallable via .func_."""
        from temporalio.contrib.langgraph._functional_activity import (
            _unwrap_task_function,
        )

        def original_func():
            return 42

        # Mock RunnableCallable with func_
        wrapper = MagicMock(spec=[])
        wrapper.func_ = original_func
        wrapper.afunc = None

        result = _unwrap_task_function(wrapper)

        assert result is original_func


class TestExecuteLangGraphTask:
    """Tests for execute_langgraph_task activity."""

    @pytest.mark.asyncio
    async def test_execute_sync_function(self) -> None:
        """Should execute sync function and return result."""
        from temporalio.contrib.langgraph._functional_activity import (
            execute_langgraph_task,
        )
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        # Create a test module with a sync function
        test_module = ModuleType("test_tasks_sync")
        setattr(test_module, "add_numbers", lambda a, b: a + b)
        sys.modules["test_tasks_sync"] = test_module

        try:
            input_data = TaskActivityInput(
                task_id="test_tasks_sync.add_numbers",
                args=(5, 3),
                kwargs={},
            )

            result = await execute_langgraph_task(input_data)

            assert result.result == 8
            assert result.error is None
        finally:
            del sys.modules["test_tasks_sync"]

    @pytest.mark.asyncio
    async def test_execute_async_function(self) -> None:
        """Should execute async function and return result."""
        from temporalio.contrib.langgraph._functional_activity import (
            execute_langgraph_task,
        )
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        # Create a test module with an async function
        test_module = ModuleType("test_tasks_async")

        async def async_multiply(a: int, b: int) -> int:
            return a * b

        setattr(test_module, "async_multiply", async_multiply)
        sys.modules["test_tasks_async"] = test_module

        try:
            input_data = TaskActivityInput(
                task_id="test_tasks_async.async_multiply",
                args=(4, 5),
                kwargs={},
            )

            result = await execute_langgraph_task(input_data)

            assert result.result == 20
            assert result.error is None
        finally:
            del sys.modules["test_tasks_async"]

    @pytest.mark.asyncio
    async def test_execute_with_kwargs(self) -> None:
        """Should pass kwargs to function correctly."""
        from temporalio.contrib.langgraph._functional_activity import (
            execute_langgraph_task,
        )
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        test_module = ModuleType("test_tasks_kwargs")
        setattr(test_module, "format_message", lambda name, greeting="Hello": f"{greeting}, {name}!")
        sys.modules["test_tasks_kwargs"] = test_module

        try:
            input_data = TaskActivityInput(
                task_id="test_tasks_kwargs.format_message",
                args=("World",),
                kwargs={"greeting": "Hi"},
            )

            result = await execute_langgraph_task(input_data)

            assert result.result == "Hi, World!"
        finally:
            del sys.modules["test_tasks_kwargs"]

    @pytest.mark.asyncio
    async def test_execute_wrapped_function(self) -> None:
        """Should unwrap and execute decorated function."""
        from temporalio.contrib.langgraph._functional_activity import (
            execute_langgraph_task,
        )
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        test_module = ModuleType("test_tasks_wrapped")

        def original_func(x: int) -> int:
            return x * 2

        # Create a mock wrapper like LangGraph's @task
        wrapper = MagicMock()
        wrapper.func = original_func

        setattr(test_module, "wrapped_func", wrapper)
        sys.modules["test_tasks_wrapped"] = test_module

        try:
            input_data = TaskActivityInput(
                task_id="test_tasks_wrapped.wrapped_func",
                args=(21,),
                kwargs={},
            )

            result = await execute_langgraph_task(input_data)

            assert result.result == 42
        finally:
            del sys.modules["test_tasks_wrapped"]

    @pytest.mark.asyncio
    async def test_execute_task_not_found_raises_error(self) -> None:
        """Should raise ApplicationError for unknown task."""
        from temporalio.contrib.langgraph._functional_activity import (
            execute_langgraph_task,
        )
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        input_data = TaskActivityInput(
            task_id="nonexistent_module_xyz.some_task",
            args=(),
            kwargs={},
        )

        with pytest.raises(ApplicationError) as exc_info:
            await execute_langgraph_task(input_data)

        assert exc_info.value.type == "TASK_NOT_FOUND"
        assert exc_info.value.non_retryable is True

    @pytest.mark.asyncio
    async def test_execute_task_with_exception_raises(self) -> None:
        """Should propagate task exceptions."""
        from temporalio.contrib.langgraph._functional_activity import (
            execute_langgraph_task,
        )
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        test_module = ModuleType("test_tasks_error")

        def failing_func() -> None:
            raise ValueError("Task failed intentionally")

        setattr(test_module, "failing_func", failing_func)
        sys.modules["test_tasks_error"] = test_module

        try:
            input_data = TaskActivityInput(
                task_id="test_tasks_error.failing_func",
                args=(),
                kwargs={},
            )

            with pytest.raises(ValueError, match="Task failed intentionally"):
                await execute_langgraph_task(input_data)
        finally:
            del sys.modules["test_tasks_error"]

    @pytest.mark.asyncio
    async def test_execute_non_retryable_error(self) -> None:
        """Should wrap LangGraph non-retryable errors as ApplicationError."""
        from temporalio.contrib.langgraph._functional_activity import (
            execute_langgraph_task,
        )
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        test_module = ModuleType("test_tasks_non_retryable")

        # Create a custom exception that matches LangGraph's non-retryable types
        class GraphRecursionError(Exception):
            pass

        def recursive_fail() -> None:
            raise GraphRecursionError("Max recursion reached")

        setattr(test_module, "recursive_fail", recursive_fail)
        sys.modules["test_tasks_non_retryable"] = test_module

        try:
            input_data = TaskActivityInput(
                task_id="test_tasks_non_retryable.recursive_fail",
                args=(),
                kwargs={},
            )

            with pytest.raises(ApplicationError) as exc_info:
                await execute_langgraph_task(input_data)

            assert exc_info.value.type == "GraphRecursionError"
            assert exc_info.value.non_retryable is True
        finally:
            del sys.modules["test_tasks_non_retryable"]

    @pytest.mark.asyncio
    async def test_execute_returns_none(self) -> None:
        """Should handle functions that return None."""
        from temporalio.contrib.langgraph._functional_activity import (
            execute_langgraph_task,
        )
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        test_module = ModuleType("test_tasks_none")
        setattr(test_module, "void_func", lambda: None)
        sys.modules["test_tasks_none"] = test_module

        try:
            input_data = TaskActivityInput(
                task_id="test_tasks_none.void_func",
                args=(),
                kwargs={},
            )

            result = await execute_langgraph_task(input_data)

            assert result.result is None
            assert result.error is None
        finally:
            del sys.modules["test_tasks_none"]

    @pytest.mark.asyncio
    async def test_execute_returns_complex_result(self) -> None:
        """Should handle complex return values."""
        from temporalio.contrib.langgraph._functional_activity import (
            execute_langgraph_task,
        )
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        test_module = ModuleType("test_tasks_complex")
        setattr(
            test_module,
            "complex_func",
            lambda: {
                "data": [1, 2, 3],
                "nested": {"key": "value"},
                "items": [{"id": 1}, {"id": 2}],
            },
        )
        sys.modules["test_tasks_complex"] = test_module

        try:
            input_data = TaskActivityInput(
                task_id="test_tasks_complex.complex_func",
                args=(),
                kwargs={},
            )

            result = await execute_langgraph_task(input_data)

            assert result.result["data"] == [1, 2, 3]
            assert result.result["nested"]["key"] == "value"
        finally:
            del sys.modules["test_tasks_complex"]
