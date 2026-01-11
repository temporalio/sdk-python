"""Unit tests for LangGraph Functional API models.

Tests for TaskActivityInput, TaskActivityOutput, and FunctionalRunnerConfig.
"""

from __future__ import annotations

from datetime import timedelta


class TestTaskActivityInput:
    """Tests for TaskActivityInput model."""

    def test_task_activity_input_basic(self) -> None:
        """TaskActivityInput should store all required fields."""
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        input_data = TaskActivityInput(
            task_id="mymodule.tasks.my_task",
            args=(1, 2, 3),
            kwargs={"key": "value"},
        )

        assert input_data.task_id == "mymodule.tasks.my_task"
        assert input_data.args == (1, 2, 3)
        assert input_data.kwargs == {"key": "value"}
        assert input_data.entrypoint_id is None

    def test_task_activity_input_with_entrypoint(self) -> None:
        """TaskActivityInput should store entrypoint_id when provided."""
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        input_data = TaskActivityInput(
            task_id="mymodule.tasks.my_task",
            args=(),
            kwargs={},
            entrypoint_id="my_entrypoint",
        )

        assert input_data.entrypoint_id == "my_entrypoint"

    def test_task_activity_input_empty_args(self) -> None:
        """TaskActivityInput should handle empty args and kwargs."""
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        input_data = TaskActivityInput(
            task_id="mymodule.tasks.no_args_task",
            args=(),
            kwargs={},
        )

        assert input_data.args == ()
        assert input_data.kwargs == {}

    def test_task_activity_input_complex_args(self) -> None:
        """TaskActivityInput should handle complex argument types."""
        from temporalio.contrib.langgraph._functional_models import TaskActivityInput

        complex_dict = {"nested": {"data": [1, 2, 3]}, "list": [{"a": 1}]}
        input_data = TaskActivityInput(
            task_id="mymodule.tasks.complex_task",
            args=("string", 42, complex_dict),
            kwargs={"optional": None, "config": {"setting": True}},
        )

        assert input_data.args[2] == complex_dict
        assert input_data.kwargs["config"]["setting"] is True


class TestTaskActivityOutput:
    """Tests for TaskActivityOutput model."""

    def test_task_activity_output_basic(self) -> None:
        """TaskActivityOutput should store result."""
        from temporalio.contrib.langgraph._functional_models import TaskActivityOutput

        output = TaskActivityOutput(result=42)

        assert output.result == 42
        assert output.error is None

    def test_task_activity_output_with_error(self) -> None:
        """TaskActivityOutput should store error message."""
        from temporalio.contrib.langgraph._functional_models import TaskActivityOutput

        output = TaskActivityOutput(result=None, error="Task failed: timeout")

        assert output.result is None
        assert output.error == "Task failed: timeout"

    def test_task_activity_output_complex_result(self) -> None:
        """TaskActivityOutput should handle complex result types."""
        from temporalio.contrib.langgraph._functional_models import TaskActivityOutput

        complex_result = {
            "data": [1, 2, 3],
            "nested": {"key": "value"},
            "items": [{"id": 1}, {"id": 2}],
        }
        output = TaskActivityOutput(result=complex_result)

        assert output.result == complex_result
        assert output.result["nested"]["key"] == "value"

    def test_task_activity_output_none_result(self) -> None:
        """TaskActivityOutput should handle None result."""
        from temporalio.contrib.langgraph._functional_models import TaskActivityOutput

        output = TaskActivityOutput(result=None)

        assert output.result is None
        assert output.error is None


class TestFunctionalRunnerConfig:
    """Tests for FunctionalRunnerConfig model."""

    def test_functional_runner_config_defaults(self) -> None:
        """FunctionalRunnerConfig should have sensible defaults."""
        from temporalio.contrib.langgraph._functional_models import (
            FunctionalRunnerConfig,
        )

        config = FunctionalRunnerConfig(entrypoint_id="my_entrypoint")

        assert config.entrypoint_id == "my_entrypoint"
        assert config.default_task_timeout == timedelta(minutes=5)
        assert config.task_options == {}

    def test_functional_runner_config_custom_timeout(self) -> None:
        """FunctionalRunnerConfig should allow custom default timeout."""
        from temporalio.contrib.langgraph._functional_models import (
            FunctionalRunnerConfig,
        )

        config = FunctionalRunnerConfig(
            entrypoint_id="my_entrypoint",
            default_task_timeout=timedelta(minutes=10),
        )

        assert config.default_task_timeout == timedelta(minutes=10)

    def test_functional_runner_config_with_task_options(self) -> None:
        """FunctionalRunnerConfig should store per-task options."""
        from temporalio.contrib.langgraph._functional_models import (
            FunctionalRunnerConfig,
        )

        config = FunctionalRunnerConfig(
            entrypoint_id="my_entrypoint",
            task_options={
                "slow_task": {"start_to_close_timeout_seconds": 900.0},
                "fast_task": {"start_to_close_timeout_seconds": 30.0},
            },
        )

        assert "slow_task" in config.task_options
        assert (
            config.task_options["slow_task"]["start_to_close_timeout_seconds"] == 900.0
        )

    def test_get_task_timeout_with_default(self) -> None:
        """get_task_timeout should return default for unknown tasks."""
        from temporalio.contrib.langgraph._functional_models import (
            FunctionalRunnerConfig,
        )

        config = FunctionalRunnerConfig(
            entrypoint_id="my_entrypoint",
            default_task_timeout=timedelta(minutes=2),
        )

        timeout = config.get_task_timeout("unknown_task")
        assert timeout == timedelta(minutes=2)

    def test_get_task_timeout_with_override(self) -> None:
        """get_task_timeout should return task-specific timeout."""
        from temporalio.contrib.langgraph._functional_models import (
            FunctionalRunnerConfig,
        )

        config = FunctionalRunnerConfig(
            entrypoint_id="my_entrypoint",
            default_task_timeout=timedelta(minutes=2),
            task_options={
                "custom_task": {"start_to_close_timeout": timedelta(minutes=10)},
            },
        )

        timeout = config.get_task_timeout("custom_task")
        assert timeout == timedelta(minutes=10)

        # Other tasks should still get default
        default_timeout = config.get_task_timeout("other_task")
        assert default_timeout == timedelta(minutes=2)
