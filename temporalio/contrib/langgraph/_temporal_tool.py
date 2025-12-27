"""Temporal-wrapped LangChain tools for durable execution."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable, Optional, Type, Union

from temporalio import workflow

if TYPE_CHECKING:
    from langchain_core.callbacks import CallbackManagerForToolRun
    from langchain_core.tools import BaseTool

    from temporalio.common import Priority, RetryPolicy
    from temporalio.workflow import ActivityCancellationType, VersioningIntent


class _TemporalToolWrapper:
    """Internal wrapper that delegates tool execution to activities."""

    def __init__(
        self,
        tool: "BaseTool",
        *,
        start_to_close_timeout: timedelta,
        schedule_to_close_timeout: Optional[timedelta] = None,
        schedule_to_start_timeout: Optional[timedelta] = None,
        heartbeat_timeout: Optional[timedelta] = None,
        task_queue: Optional[str] = None,
        retry_policy: Optional["RetryPolicy"] = None,
        cancellation_type: Optional["ActivityCancellationType"] = None,
        versioning_intent: Optional["VersioningIntent"] = None,
        priority: Optional["Priority"] = None,
    ) -> None:
        self._tool = tool
        self._activity_options: dict[str, Any] = {
            "start_to_close_timeout": start_to_close_timeout,
        }
        if schedule_to_close_timeout is not None:
            self._activity_options["schedule_to_close_timeout"] = (
                schedule_to_close_timeout
            )
        if schedule_to_start_timeout is not None:
            self._activity_options["schedule_to_start_timeout"] = (
                schedule_to_start_timeout
            )
        if heartbeat_timeout is not None:
            self._activity_options["heartbeat_timeout"] = heartbeat_timeout
        if task_queue is not None:
            self._activity_options["task_queue"] = task_queue
        if retry_policy is not None:
            self._activity_options["retry_policy"] = retry_policy
        if cancellation_type is not None:
            self._activity_options["cancellation_type"] = cancellation_type
        if versioning_intent is not None:
            self._activity_options["versioning_intent"] = versioning_intent
        if priority is not None:
            self._activity_options["priority"] = priority

    def _create_wrapper_class(self) -> Type["BaseTool"]:
        """Create a dynamic BaseTool subclass wrapping the original tool."""
        # Import here to avoid workflow sandbox issues
        with workflow.unsafe.imports_passed_through():
            from langchain_core.tools import BaseTool
            from pydantic import ConfigDict

        original_tool = self._tool
        activity_options = self._activity_options

        # Store values in closure to avoid Pydantic field issues
        _tool_name = original_tool.name
        _tool_description = original_tool.description
        _tool_args_schema = getattr(original_tool, "args_schema", None)
        _tool_return_direct = getattr(original_tool, "return_direct", False)

        class TemporalToolWrapper(BaseTool):  # type: ignore[valid-type, misc]
            """Dynamic wrapper class for temporal tool execution."""

            # Use Pydantic ConfigDict to allow arbitrary types
            model_config = ConfigDict(arbitrary_types_allowed=True)

            # Properly annotated fields to satisfy Pydantic v2
            name: str = _tool_name
            description: str = _tool_description
            args_schema: Any = _tool_args_schema
            return_direct: bool = _tool_return_direct

            # Store reference to original as private class attrs (not Pydantic fields)
            _original_tool: Any = original_tool
            _activity_options: Any = activity_options

            def _run(
                self,
                *args: Any,
                run_manager: Optional["CallbackManagerForToolRun"] = None,
                **kwargs: Any,
            ) -> Any:
                """Synchronous execution - delegates to async."""
                import asyncio

                return asyncio.get_event_loop().run_until_complete(
                    self._arun(*args, run_manager=run_manager, **kwargs)
                )

            async def _arun(
                self,
                *args: Any,
                run_manager: Optional["CallbackManagerForToolRun"] = None,
                **kwargs: Any,
            ) -> Any:
                """Async execution - routes to activity when in workflow."""
                # Check if we're in a workflow
                if not workflow.in_workflow():
                    # Outside workflow, run directly
                    return await self._original_tool.ainvoke(
                        input=kwargs if kwargs else (args[0] if args else {}),
                    )

                # In workflow, execute as activity
                with workflow.unsafe.imports_passed_through():
                    from temporalio.contrib.langgraph._activities import execute_tool
                    from temporalio.contrib.langgraph._models import ToolActivityInput

                # Build activity input
                # Handle both positional and keyword arguments
                tool_input: dict[str, Any]
                if args:
                    # If single string arg, it's the tool input
                    if len(args) == 1 and isinstance(args[0], (str, dict)):
                        tool_input = (
                            args[0] if isinstance(args[0], dict) else {"input": args[0]}
                        )
                    else:
                        tool_input = {"args": args, **kwargs}
                else:
                    tool_input = kwargs

                activity_input = ToolActivityInput(
                    tool_name=self.name,
                    tool_input=tool_input,
                )

                # Execute as activity
                result = await workflow.execute_activity(
                    execute_tool,
                    activity_input,
                    **self._activity_options,
                )

                return result.output

        return TemporalToolWrapper

    def wrap(self) -> "BaseTool":
        """Create and return the wrapped tool instance."""
        wrapper_class = self._create_wrapper_class()
        return wrapper_class()


def temporal_tool(
    tool: Union["BaseTool", Callable[..., Any]],
    *,
    start_to_close_timeout: timedelta = timedelta(minutes=5),
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    task_queue: Optional[str] = None,
    retry_policy: Optional["RetryPolicy"] = None,
    cancellation_type: Optional["ActivityCancellationType"] = None,
    versioning_intent: Optional["VersioningIntent"] = None,
    priority: Optional["Priority"] = None,
) -> "BaseTool":
    """Wrap a LangChain tool to execute as a Temporal activity.

    .. warning::
        This API is experimental and may change in future versions.

    Wrapped tools execute durably as activities with retries and failure recovery.
    The tool's metadata (name, description, args_schema) is preserved.
    """
    # Import here to avoid issues at module load time
    with workflow.unsafe.imports_passed_through():
        from langchain_core.tools import BaseTool, StructuredTool

    # Convert callable to tool if needed
    if callable(tool) and not isinstance(tool, BaseTool):
        # Check if it's a @tool decorated function
        if hasattr(tool, "name") and hasattr(tool, "description"):
            # Already a tool-like object, try to use it directly
            pass
        else:
            # Convert plain function to StructuredTool
            tool = StructuredTool.from_function(tool)

    if not isinstance(tool, BaseTool):
        raise TypeError(
            f"Expected BaseTool or callable, got {type(tool).__name__}. "
            "Use @tool decorator or StructuredTool.from_function() to create a tool."
        )

    # Register tool in global registry for activity lookup
    from temporalio.contrib.langgraph._tool_registry import register_tool

    register_tool(tool)

    # Create and return wrapper
    wrapper = _TemporalToolWrapper(
        tool,
        start_to_close_timeout=start_to_close_timeout,
        schedule_to_close_timeout=schedule_to_close_timeout,
        schedule_to_start_timeout=schedule_to_start_timeout,
        heartbeat_timeout=heartbeat_timeout,
        task_queue=task_queue,
        retry_policy=retry_policy,
        cancellation_type=cancellation_type,
        versioning_intent=versioning_intent,
        priority=priority,
    )

    return wrapper.wrap()
