"""Unit tests for LangGraph Functional API runner.

Tests for TemporalFunctionalRunner, compile_functional, and helper functions.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class TestGetTaskIdentifier:
    """Tests for _get_task_identifier helper."""

    def test_regular_function(self) -> None:
        """Should return module.qualname for regular functions."""
        from temporalio.contrib.langgraph._functional_runner import _get_task_identifier

        # Use a module-level function for this test (json.dumps is a good example)
        import json

        result = _get_task_identifier(json.dumps)

        # Should be module.qualname
        assert result is not None
        assert result == "json.dumps"

    def test_lambda_returns_none(self) -> None:
        """Should return None for lambda (contains <locals>)."""
        from temporalio.contrib.langgraph._functional_runner import _get_task_identifier

        func = lambda x: x  # noqa: E731

        result = _get_task_identifier(func)

        # Lambda qualnames contain <lambda> or <locals>
        # Depending on Python version, this may return None
        assert result is None or "<lambda>" in result

    def test_nested_function_returns_none(self) -> None:
        """Should return None for nested functions (closures)."""
        from temporalio.contrib.langgraph._functional_runner import _get_task_identifier

        def outer() -> Any:
            def inner() -> None:
                pass

            return inner

        func = outer()
        result = _get_task_identifier(func)

        assert result is None

    def test_main_module_returns_none(self) -> None:
        """Should return None for functions in __main__ module."""
        from temporalio.contrib.langgraph._functional_runner import _get_task_identifier

        def sample_func() -> None:
            pass

        # Simulate __main__ module
        sample_func.__module__ = "__main__"
        result = _get_task_identifier(sample_func)

        assert result is None

    def test_function_without_module_returns_none(self) -> None:
        """Should return None if function has no module."""
        from temporalio.contrib.langgraph._functional_runner import _get_task_identifier

        func = MagicMock(spec=[])  # No __module__ attribute
        result = _get_task_identifier(func)

        assert result is None


class TestContextDetection:
    """Tests for workflow/activity context detection."""

    def test_is_in_workflow_context_outside_workflow(self) -> None:
        """Should return False when not in workflow."""
        from temporalio.contrib.langgraph._functional_runner import _is_in_workflow_context

        # Outside workflow context
        result = _is_in_workflow_context()

        assert result is False

    def test_is_in_activity_context_outside_activity(self) -> None:
        """Should return False when not in activity."""
        from temporalio.contrib.langgraph._functional_runner import _is_in_activity_context

        # Outside activity context
        result = _is_in_activity_context()

        assert result is False

    @patch("temporalio.contrib.langgraph._functional_runner.workflow")
    def test_is_in_workflow_context_in_workflow(self, mock_workflow: MagicMock) -> None:
        """Should return True when in workflow sandbox."""
        from temporalio.contrib.langgraph._functional_runner import _is_in_workflow_context

        mock_workflow.unsafe.in_sandbox.return_value = True

        result = _is_in_workflow_context()

        assert result is True

    @patch("temporalio.contrib.langgraph._functional_runner.activity")
    def test_is_in_activity_context_in_activity(self, mock_activity: MagicMock) -> None:
        """Should return True when activity.info() succeeds."""
        from temporalio.contrib.langgraph._functional_runner import _is_in_activity_context

        mock_activity.info.return_value = MagicMock()

        result = _is_in_activity_context()

        assert result is True


class TestTemporalFunctionalRunner:
    """Tests for TemporalFunctionalRunner class."""

    def setup_method(self) -> None:
        """Clear registry before each test."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_global_entrypoint_registry,
        )

        get_global_entrypoint_registry().clear()

    def teardown_method(self) -> None:
        """Clear registry after each test."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_global_entrypoint_registry,
        )

        get_global_entrypoint_registry().clear()

    def test_initialization_defaults(self) -> None:
        """Runner should initialize with default values."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(entrypoint_id="test_ep")

        assert runner._entrypoint_id == "test_ep"
        assert runner._default_task_timeout == timedelta(minutes=5)
        assert runner._task_options == {}

    def test_initialization_with_custom_timeout(self) -> None:
        """Runner should accept custom default timeout."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(
            entrypoint_id="test_ep",
            default_task_timeout=timedelta(minutes=10),
        )

        assert runner._default_task_timeout == timedelta(minutes=10)

    def test_initialization_with_task_options(self) -> None:
        """Runner should store per-task options."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(
            entrypoint_id="test_ep",
            task_options={
                "slow_task": {"start_to_close_timeout": timedelta(minutes=15)},
            },
        )

        assert "slow_task" in runner._task_options

    def test_merges_registered_task_options(self) -> None:
        """Runner should merge with options from registry."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint(
            "test_ep",
            mock_pregel,
            per_task_options={"registered_task": {"timeout": 100}},
        )

        runner = TemporalFunctionalRunner(
            entrypoint_id="test_ep",
            task_options={"local_task": {"timeout": 200}},
        )

        assert "registered_task" in runner._task_options
        assert "local_task" in runner._task_options

    def test_get_pregel(self) -> None:
        """_get_pregel should return registered pregel."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(entrypoint_id="test_ep")
        result = runner._get_pregel()

        assert result is mock_pregel

    def test_get_task_timeout_default(self) -> None:
        """_get_task_timeout should return default for unknown task."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(
            entrypoint_id="test_ep",
            default_task_timeout=timedelta(minutes=7),
        )

        result = runner._get_task_timeout("unknown_task")

        assert result == timedelta(minutes=7)

    def test_get_task_timeout_override(self) -> None:
        """_get_task_timeout should return task-specific timeout."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(
            entrypoint_id="test_ep",
            task_options={
                "custom_task": {"start_to_close_timeout": timedelta(minutes=20)},
            },
        )

        result = runner._get_task_timeout("custom_task")

        assert result == timedelta(minutes=20)

    def test_get_task_timeout_from_seconds(self) -> None:
        """_get_task_timeout should convert numeric seconds to timedelta."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(
            entrypoint_id="test_ep",
            task_options={
                "numeric_task": {"start_to_close_timeout": 120},  # seconds
            },
        )

        result = runner._get_task_timeout("numeric_task")

        assert result == timedelta(seconds=120)

    def test_execute_task_inline_sync(self) -> None:
        """_execute_task_inline should execute sync function."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(entrypoint_id="test_ep")

        def add(a: int, b: int) -> int:
            return a + b

        result = runner._execute_task_inline(add, (3, 4), {})

        assert result.done() is True
        assert result.result() == 7

    def test_execute_task_inline_with_exception(self) -> None:
        """_execute_task_inline should capture exceptions."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(entrypoint_id="test_ep")

        def failing() -> None:
            raise ValueError("Test error")

        result = runner._execute_task_inline(failing, (), {})

        assert result.done() is True
        assert result.exception() is not None
        with pytest.raises(ValueError, match="Test error"):
            result.result()

    def test_execute_task_inline_unwraps_func(self) -> None:
        """_execute_task_inline should unwrap .func attribute."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(entrypoint_id="test_ep")

        def actual_func(x: int) -> int:
            return x * 2

        wrapper = MagicMock()
        wrapper.func = actual_func

        result = runner._execute_task_inline(wrapper, (5,), {})

        assert result.result() == 10

    def test_get_graph(self) -> None:
        """get_graph should return the pregel."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(entrypoint_id="test_ep")
        result = runner.get_graph()

        assert result is mock_pregel


class TestCompileFunctional:
    """Tests for compile_functional factory function."""

    def setup_method(self) -> None:
        """Clear registry before each test."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_global_entrypoint_registry,
        )

        get_global_entrypoint_registry().clear()

    def teardown_method(self) -> None:
        """Clear registry after each test."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_global_entrypoint_registry,
        )

        get_global_entrypoint_registry().clear()

    def test_returns_runner(self) -> None:
        """compile_functional should return TemporalFunctionalRunner."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
            compile_functional,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        result = compile_functional("test_ep")

        assert isinstance(result, TemporalFunctionalRunner)
        assert result._entrypoint_id == "test_ep"

    def test_passes_timeout(self) -> None:
        """compile_functional should pass timeout to runner."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import compile_functional

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        result = compile_functional(
            "test_ep",
            default_task_timeout=timedelta(minutes=15),
        )

        assert result._default_task_timeout == timedelta(minutes=15)

    def test_passes_task_options(self) -> None:
        """compile_functional should pass task_options to runner."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import compile_functional

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        result = compile_functional(
            "test_ep",
            task_options={"my_task": {"timeout": 100}},
        )

        assert "my_task" in result._task_options


class TestTemporalCallCallback:
    """Tests for the CONFIG_KEY_CALL callback creation."""

    def setup_method(self) -> None:
        """Clear registry before each test."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_global_entrypoint_registry,
        )

        get_global_entrypoint_registry().clear()

    def teardown_method(self) -> None:
        """Clear registry after each test."""
        from temporalio.contrib.langgraph._functional_registry import (
            get_global_entrypoint_registry,
        )

        get_global_entrypoint_registry().clear()

    def test_callback_raises_for_non_importable_function(self) -> None:
        """Callback should raise ValueError for lambdas/closures."""
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(entrypoint_id="test_ep")
        callback = runner._create_temporal_call_callback()

        def outer() -> Any:
            def inner() -> None:
                pass

            return inner

        nested_func = outer()

        with pytest.raises(ValueError, match="not importable"):
            callback(nested_func, ((), {}))

    @patch("temporalio.contrib.langgraph._functional_runner._get_task_identifier")
    @patch("temporalio.contrib.langgraph._functional_runner._is_in_workflow_context")
    @patch("temporalio.contrib.langgraph._functional_runner._is_in_activity_context")
    def test_callback_executes_inline_outside_temporal(
        self,
        mock_activity_ctx: MagicMock,
        mock_workflow_ctx: MagicMock,
        mock_get_task_id: MagicMock,
    ) -> None:
        """Callback should execute inline when not in Temporal context."""
        from temporalio.contrib.langgraph._functional_future import InlineFuture
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_workflow_ctx.return_value = False
        mock_activity_ctx.return_value = False
        mock_get_task_id.return_value = "test_module.add"

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(entrypoint_id="test_ep")
        callback = runner._create_temporal_call_callback()

        def add(a: int, b: int) -> int:
            return a + b

        result = callback(add, ((2, 3), {}))

        assert isinstance(result, InlineFuture)
        assert result.result() == 5

    @patch("temporalio.contrib.langgraph._functional_runner._get_task_identifier")
    @patch("temporalio.contrib.langgraph._functional_runner._is_in_workflow_context")
    @patch("temporalio.contrib.langgraph._functional_runner._is_in_activity_context")
    def test_callback_executes_inline_in_activity(
        self,
        mock_activity_ctx: MagicMock,
        mock_workflow_ctx: MagicMock,
        mock_get_task_id: MagicMock,
    ) -> None:
        """Callback should execute inline when in activity context."""
        from temporalio.contrib.langgraph._functional_future import InlineFuture
        from temporalio.contrib.langgraph._functional_registry import register_entrypoint
        from temporalio.contrib.langgraph._functional_runner import (
            TemporalFunctionalRunner,
        )

        mock_workflow_ctx.return_value = False
        mock_activity_ctx.return_value = True
        mock_get_task_id.return_value = "test_module.multiply"

        mock_pregel = MagicMock()
        register_entrypoint("test_ep", mock_pregel)

        runner = TemporalFunctionalRunner(entrypoint_id="test_ep")
        callback = runner._create_temporal_call_callback()

        def multiply(a: int, b: int) -> int:
            return a * b

        result = callback(multiply, ((4, 5), {}))

        assert isinstance(result, InlineFuture)
        assert result.result() == 20
