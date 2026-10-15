"""Modal-tuned defaults for Temporal worker and client configuration."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from temporalio.worker import PollerBehaviorSimpleMaximum, WorkerConfig

DEFAULT_MAX_CONCURRENT_ACTIVITIES: int = 2
DEFAULT_MAX_CONCURRENT_WORKFLOW_TASKS: int = 10
DEFAULT_MAX_CONCURRENT_LOCAL_ACTIVITIES: int = 2
DEFAULT_MAX_CONCURRENT_NEXUS_TASKS: int = 5
DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT: timedelta = timedelta(seconds=5)
DEFAULT_SHUTDOWN_HOOK_BUFFER: timedelta = timedelta(seconds=2)
DEFAULT_MAX_CACHED_WORKFLOWS: int = 30
DEFAULT_EXECUTION_TIMEOUT: timedelta = timedelta(minutes=5)
MIN_FUNCTION_TIMEOUT_HEADROOM: timedelta = timedelta(seconds=15)
"""Minimum time by which the Modal Function timeout must exceed the worker budget."""

DEFAULT_WORKFLOW_TASK_POLLER_BEHAVIOR = PollerBehaviorSimpleMaximum(maximum=2)
DEFAULT_ACTIVITY_TASK_POLLER_BEHAVIOR = PollerBehaviorSimpleMaximum(maximum=1)
DEFAULT_NEXUS_TASK_POLLER_BEHAVIOR = PollerBehaviorSimpleMaximum(maximum=1)

ENV_TASK_QUEUE = "TEMPORAL_TASK_QUEUE"
ENV_MODAL_TASK_ID = "MODAL_TASK_ID"
ENV_CONFIG_FILE = "TEMPORAL_CONFIG_FILE"
DEFAULT_CONFIG_FILE = "temporal.toml"


def apply_modal_worker_defaults(config: WorkerConfig) -> None:
    """Apply conservative defaults suitable for a Modal Function.

    Values already present in *config* are preserved, except eager activity
    execution, which is always disabled for an ephemeral worker.
    """
    config.setdefault("max_concurrent_activities", DEFAULT_MAX_CONCURRENT_ACTIVITIES)
    config.setdefault(
        "max_concurrent_workflow_tasks", DEFAULT_MAX_CONCURRENT_WORKFLOW_TASKS
    )
    config.setdefault(
        "max_concurrent_local_activities", DEFAULT_MAX_CONCURRENT_LOCAL_ACTIVITIES
    )
    config.setdefault("max_concurrent_nexus_tasks", DEFAULT_MAX_CONCURRENT_NEXUS_TASKS)
    config.setdefault("graceful_shutdown_timeout", DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT)
    config.setdefault("max_cached_workflows", DEFAULT_MAX_CACHED_WORKFLOWS)
    config.setdefault(
        "workflow_task_poller_behavior", DEFAULT_WORKFLOW_TASK_POLLER_BEHAVIOR
    )
    config.setdefault(
        "activity_task_poller_behavior", DEFAULT_ACTIVITY_TASK_POLLER_BEHAVIOR
    )
    config.setdefault("nexus_task_poller_behavior", DEFAULT_NEXUS_TASK_POLLER_BEHAVIOR)
    config["disable_eager_activity_execution"] = True


def build_modal_identity(function_call_id: str, container_id: str) -> str:
    """Build ``<function call ID>@<container ID>`` for a worker invocation."""
    return f"{function_call_id or 'unknown'}@{container_id or 'unknown'}"


def modal_default_config_file_path(
    getenv: Callable[[str], str | None] = os.environ.get,
) -> Path:
    """Return the Temporal config path for a Modal Function.

    ``TEMPORAL_CONFIG_FILE`` takes precedence over ``temporal.toml`` in the
    current working directory.
    """
    config_file = getenv(ENV_CONFIG_FILE)
    return Path(config_file) if config_file else Path(DEFAULT_CONFIG_FILE)
