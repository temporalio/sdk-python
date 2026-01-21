"""Dataclass models for LangGraph Functional API integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any


@dataclass
class TaskActivityInput:
    """Input for the functional task execution activity."""

    task_id: str
    """Full module path to the task function (e.g., 'mymodule.tasks.research_topic')."""

    args: tuple[Any, ...]
    """Positional arguments for the task."""

    kwargs: dict[str, Any]
    """Keyword arguments for the task."""

    entrypoint_id: str | None = None
    """Entrypoint ID for context (optional)."""


@dataclass
class TaskActivityOutput:
    """Output from the functional task execution activity."""

    result: Any
    """The return value from the task."""

    error: str | None = None
    """Error message if the task failed."""


@dataclass
class FunctionalRunnerConfig:
    """Configuration for the functional API runner."""

    entrypoint_id: str
    """ID of the entrypoint being executed."""

    default_task_timeout: timedelta = field(
        default_factory=lambda: timedelta(minutes=5)
    )
    """Default timeout for task activities (5 minutes)."""

    task_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Per-task activity options, keyed by task name."""

    def get_task_timeout(self, task_name: str) -> timedelta:
        """Get timeout for a specific task."""
        task_opts = self.task_options.get(task_name, {})
        return task_opts.get("start_to_close_timeout", self.default_task_timeout)
