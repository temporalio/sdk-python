"""Lambda-tuned defaults for Temporal worker and client configuration."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from temporalio.worker import PollerBehaviorSimpleMaximum, WorkerConfig

# ---- Lambda-tuned worker defaults ----
# Conservative concurrency limits suited to Lambda's resource constraints.

DEFAULT_MAX_CONCURRENT_ACTIVITIES: int = 2
DEFAULT_MAX_CONCURRENT_WORKFLOW_TASKS: int = 10
DEFAULT_MAX_CONCURRENT_LOCAL_ACTIVITIES: int = 2
DEFAULT_MAX_CONCURRENT_NEXUS_TASKS: int = 5
DEFAULT_GRACEFUL_SHUTDOWN_TIMEOUT: timedelta = timedelta(seconds=5)
DEFAULT_SHUTDOWN_HOOK_BUFFER: timedelta = timedelta(seconds=2)
DEFAULT_MAX_CACHED_WORKFLOWS: int = 30

DEFAULT_WORKFLOW_TASK_POLLER_BEHAVIOR = PollerBehaviorSimpleMaximum(maximum=2)
DEFAULT_ACTIVITY_TASK_POLLER_BEHAVIOR = PollerBehaviorSimpleMaximum(maximum=1)
DEFAULT_NEXUS_TASK_POLLER_BEHAVIOR = PollerBehaviorSimpleMaximum(maximum=1)

# ---- Environment variable names ----
ENV_TASK_QUEUE = "TEMPORAL_TASK_QUEUE"
ENV_LAMBDA_TASK_ROOT = "LAMBDA_TASK_ROOT"
ENV_CONFIG_FILE = "TEMPORAL_CONFIG_FILE"
DEFAULT_CONFIG_FILE = "temporal.toml"


def apply_lambda_worker_defaults(config: WorkerConfig) -> None:
    """Apply Lambda-appropriate defaults to worker config.

    Only sets values that have not already been set (i.e. are absent from *config*).
    ``disable_eager_activity_execution`` is always set to ``True``.
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
    # Always disable eager activities in Lambda.
    config["disable_eager_activity_execution"] = True


def build_lambda_identity(request_id: str, function_arn: str) -> str:
    """Build a worker identity string from the Lambda invocation context.

    Format: ``<request_id>@<function_arn>``.
    """
    return f"{request_id or 'unknown'}@{function_arn or 'unknown'}"


def lambda_default_config_file_path(
    getenv: Callable[[str], str] = os.environ.get,  # type: ignore[assignment]
) -> Path:
    """Return the config file path for a Lambda environment.

    Resolution order:

    1. ``TEMPORAL_CONFIG_FILE`` env var, if set.
    2. ``temporal.toml`` in ``$LAMBDA_TASK_ROOT`` (typically ``/var/task``).
    3. ``temporal.toml`` in the current working directory.
    """
    config_file = getenv(ENV_CONFIG_FILE)
    if config_file:
        return Path(config_file)
    root = getenv(ENV_LAMBDA_TASK_ROOT) or "."
    return Path(root) / DEFAULT_CONFIG_FILE
