from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
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

from ._temporal_activity_tool import TemporalActivityTool
from ._temporal_mcp_client import TemporalMCPClient
from ._temporal_model import TemporalModel


class StrandsPlugin(SimplePlugin):
    """Temporal Worker plugin for the Strands Agents SDK.

    Configures sandbox passthrough for ``strands``, ``strands_tools``, ``mcp``,
    and ``temporalio.contrib.strands`` (so the MCP tool cache is visible to
    workflow code), and swaps in ``pydantic_data_converter`` so structured
    outputs serialize.

    When ``model`` is supplied, calls its ``model_factory`` once on the worker
    to construct the real model, then registers the model invocation activities
    against it. The same :class:`TemporalModel` is also passed to
    ``Agent(model=...)`` inside the workflow.

    When ``mcp_clients`` is supplied, registers per-server ``{server}-call-tool``
    activities and, at worker startup, connects to each MCP server and caches
    its tool list. Workflow-side ``TemporalMCPClient.load_tools()`` reads from
    the cache. The plugin raises if any two clients share the same ``server``.
    """

    def __init__(
        self,
        *,
        model: TemporalModel | None = None,
        mcp_clients: list[TemporalMCPClient] = [],
    ) -> None:
        activities: list[Callable] = []
        if model is not None:
            ma = model._build_activity()
            activities.extend([ma.invoke_model, ma.invoke_model_streaming])

        names = [c.server for c in mcp_clients]
        if len(names) != len(set(names)):
            raise ValueError(
                "Duplicate MCP server names in mcp_clients; each must be unique."
            )
        for c in mcp_clients:
            activities.extend(c._get_activities())

        @asynccontextmanager
        async def run_context() -> AsyncIterator[None]:
            for c in mcp_clients:
                await c._populate_cache()
            try:
                yield
            finally:
                for c in mcp_clients:
                    c._clear_cache()

        super().__init__(
            "aws.StrandsPlugin",
            workflow_runner=_workflow_runner,
            data_converter=_data_converter,
            activities=activities or None,
            run_context=run_context,
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
    return TemporalActivityTool(activity_fn, options)


def _workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
    if not runner:
        raise ValueError("No WorkflowRunner provided to the Strands plugin.")
    if isinstance(runner, SandboxedWorkflowRunner):
        return replace(
            runner,
            restrictions=runner.restrictions.with_passthrough_modules(
                "strands",
                "strands_tools",
                "mcp",
                "temporalio.contrib.strands",
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
