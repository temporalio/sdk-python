from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from typing import Any

from strands.types.tools import AgentTool

from temporalio.common import Priority, RetryPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.workflow import ActivityCancellationType, VersioningIntent

from ._temporal_activity_tool import _TemporalActivityTool


class StrandsPlugin(SimplePlugin):
    """Temporal Worker plugin for the Strands Agents SDK.

    Configures sandbox passthrough for ``strands`` and ``strands_tools`` and
    swaps in ``pydantic_data_converter`` so structured outputs serialize.
    """

    def __init__(self) -> None:
        super().__init__(
            "aws.StrandsPlugin",
            workflow_runner=_workflow_runner,
            data_converter=_data_converter,
        )


def activity_as_tool(
    activity_fn: Callable,
    *,
    task_queue: str | None = None,
    schedule_to_close_timeout: timedelta | None = None,
    schedule_to_start_timeout: timedelta | None = None,
    start_to_close_timeout: timedelta | None = None,
    heartbeat_timeout: timedelta | None = None,
    retry_policy: RetryPolicy | None = None,
    cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
    activity_id: str | None = None,
    versioning_intent: VersioningIntent | None = None,
    summary: str | None = None,
    priority: Priority = Priority.default,
) -> AgentTool:
    """Wrap a Temporal activity as a Strands tool.

    ``activity_fn`` must be decorated by ``@activity.defn``. All keyword
    arguments are forwarded to ``workflow.execute_activity``.
    """
    options: dict[str, Any] = {
        "task_queue": task_queue,
        "schedule_to_close_timeout": schedule_to_close_timeout,
        "schedule_to_start_timeout": schedule_to_start_timeout,
        "start_to_close_timeout": start_to_close_timeout,
        "heartbeat_timeout": heartbeat_timeout,
        "retry_policy": retry_policy,
        "cancellation_type": cancellation_type,
        "activity_id": activity_id,
        "versioning_intent": versioning_intent,
        "summary": summary,
        "priority": priority,
    }
    return _TemporalActivityTool(activity_fn, options)


def _workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
    if not runner:
        raise ValueError("No WorkflowRunner provided to the Strands plugin.")
    if isinstance(runner, SandboxedWorkflowRunner):
        return replace(
            runner,
            restrictions=runner.restrictions.with_passthrough_modules(
                "strands",
                "strands_tools",
            ),
        )
    return runner


def _data_converter(converter: DataConverter | None) -> DataConverter:
    if (
        converter is None
        or converter.payload_converter_class is DefaultPayloadConverter
    ):
        return pydantic_data_converter
    return converter
