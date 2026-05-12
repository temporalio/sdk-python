import asyncio
import functools
import inspect
from dataclasses import replace
from datetime import timedelta
from types import ModuleType
from typing import Any, Callable

from strands import tool as strands_tool
from strands.tools import ToolProvider
from strands.tools.decorator import DecoratedFunctionTool

from temporalio import activity, workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.workflow import ActivityCancellationType, VersioningIntent


class StrandsPlugin(SimplePlugin):
    """Temporal Worker plugin for the Strands Agents SDK.

    Marks ``strands`` as a sandbox passthrough module and registers any
    nondeterministic tools as Temporal activities so they can be invoked
    from inside workflows.
    """

    def __init__(
        self,
        activity_tools: list[ToolProvider | Any] | None = None,
    ):
        """Initialize the plugin.

        Args:
            activity_tools: Tools to register as Temporal activities. Accepts
                the same forms as ``strands.Agent(tools=...)``: plain
                functions, ``@tool``-decorated functions, or imported
                ``strands_tools`` submodules.
        """
        super().__init__(
            "aws.StrandsPlugin",
            activities=[_build_activity(tool) for tool in activity_tools or []],
            workflow_runner=_workflow_runner,
            data_converter=_data_converter,
        )


def as_activity(
    tool: ToolProvider | Any,
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
) -> DecoratedFunctionTool:
    """Wrap a Strands tool to dispatch through a Temporal activity.

    Accepts the same forms as ``strands.Agent(tools=...)``. The returned
    tool has the same name and schema as the original, but its body calls
    ``workflow.execute_activity`` instead of running the function inline.
    The activity itself must be registered on the worker via
    ``StrandsPlugin(activity_tools=[fn])``.

    All keyword arguments are forwarded to ``workflow.execute_activity``;
    refer to its documentation for details.
    """
    fn = _unwrap_tool(tool)
    sig = inspect.signature(fn)
    activity_name = fn.__name__
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

    @functools.wraps(fn)
    async def proxy(*args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        positional = list(bound.arguments.values())
        if not positional:
            return await workflow.execute_activity(activity_name, **options)
        if len(positional) == 1:
            return await workflow.execute_activity(
                activity_name, positional[0], **options
            )
        return await workflow.execute_activity(
            activity_name, args=positional, **options
        )

    proxy.__signature__ = sig  # type: ignore[attr-defined]
    return strands_tool(proxy)


def _build_activity(tool: ToolProvider | Any) -> Callable:
    fn = _unwrap_tool(tool)
    return activity.defn(name=fn.__name__)(_ensure_async(fn))


def _unwrap_tool(tool: ToolProvider | Any) -> Callable:
    if isinstance(tool, ModuleType):
        name = tool.__name__.rsplit(".", 1)[-1]
        fn = getattr(tool, name)
    else:
        fn = tool
    if isinstance(fn, DecoratedFunctionTool):
        return fn._tool_func
    if not callable(fn):
        raise TypeError(f"Cannot wrap {fn!r} as a Temporal activity")
    return fn


def _ensure_async(fn: Callable) -> Callable:
    """Return an async-compatible version of ``fn``.

    Temporal's Worker rejects sync activity functions unless an
    ``activity_executor`` is configured. Most ``strands_tools`` functions
    (e.g. ``current_time``, ``calculator``) are sync, so we wrap them in an
    ``asyncio.to_thread`` call instead of requiring the user to wire up an
    executor on the Worker.
    """
    if inspect.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    return wrapper


def _workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
    """Add ``strands`` and ``strands_tools`` to the sandbox passthrough list."""
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
    """Default to ``pydantic_data_converter`` when the user hasn't overridden.

    Strands optionally surfaces pydantic ``BaseModel`` values (e.g.
    ``result.structured_output``) that the default Temporal converter can't
    serialize. ``pydantic_data_converter`` is a strict superset of the
    default, so this is safe even for workflows that never touch pydantic.
    """
    if (
        converter is None
        or converter.payload_converter_class is DefaultPayloadConverter
    ):
        return pydantic_data_converter
    return converter
