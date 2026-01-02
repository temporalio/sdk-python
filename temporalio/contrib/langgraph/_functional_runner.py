"""Runner for LangGraph functional API with Temporal integration.

This module provides the TemporalFunctionalRunner that wraps a LangGraph
@entrypoint (Pregel object) and injects CONFIG_KEY_CALL to route task calls
to Temporal activities.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

from temporalio import activity, workflow
from temporalio.contrib.langgraph._functional_activity import execute_langgraph_task
from temporalio.contrib.langgraph._functional_future import (
    InlineFuture,
    TemporalTaskFuture,
)
from temporalio.contrib.langgraph._functional_models import TaskActivityInput
from temporalio.contrib.langgraph._functional_registry import (
    get_entrypoint,
    get_entrypoint_default_options,
    get_entrypoint_task_options,
)

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.pregel import Pregel
    from langgraph.types import Command

logger = logging.getLogger(__name__)


def _get_task_identifier(func: Any) -> str | None:
    """Get the module.qualname identifier for a function.

    This mirrors LangGraph's identifier() function behavior.
    Returns None for functions that can't be imported (lambdas, closures, __main__).
    """
    # Get module and qualname
    module = getattr(func, "__module__", None)
    qualname = getattr(func, "__qualname__", None) or getattr(func, "__name__", None)

    if module is None or qualname is None:
        return None

    # __main__ functions can't be imported by workers
    if module == "__main__":
        return None

    # Local functions (closures) can't be imported
    if "<locals>" in qualname:
        return None

    return f"{module}.{qualname}"


def _is_in_workflow_context() -> bool:
    """Check if we're currently in a Temporal workflow context."""
    try:
        return workflow.unsafe.in_sandbox()
    except Exception:
        return False


def _is_in_activity_context() -> bool:
    """Check if we're currently in a Temporal activity context."""
    try:
        activity.info()
        return True
    except Exception:
        return False


class TemporalFunctionalRunner:
    """Runner that executes LangGraph entrypoints with Temporal task routing.

    This wraps a Pregel object (from @entrypoint) and injects a custom
    CONFIG_KEY_CALL callback that routes task calls to Temporal activities.
    """

    def __init__(
        self,
        entrypoint_id: str,
        default_task_timeout: timedelta = timedelta(minutes=5),
        task_options: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the runner.

        Args:
            entrypoint_id: ID of the registered entrypoint.
            default_task_timeout: Default timeout for task activities.
            task_options: Per-task activity options.
        """
        self._entrypoint_id = entrypoint_id
        self._default_task_timeout = default_task_timeout
        self._task_options = task_options or {}

        # Merge with registered options
        registered_options = get_entrypoint_task_options(entrypoint_id)
        for task_name, opts in registered_options.items():
            if task_name not in self._task_options:
                self._task_options[task_name] = opts

        registered_defaults = get_entrypoint_default_options(entrypoint_id)
        if "start_to_close_timeout" in registered_defaults:
            self._default_task_timeout = registered_defaults["start_to_close_timeout"]

    def _get_pregel(self) -> Pregel:
        """Get the Pregel object for this entrypoint."""
        return get_entrypoint(self._entrypoint_id)

    def _get_task_timeout(self, task_name: str) -> timedelta:
        """Get the timeout for a specific task."""
        task_opts = self._task_options.get(task_name, {})
        timeout = task_opts.get("start_to_close_timeout", self._default_task_timeout)
        if isinstance(timeout, (int, float)):
            return timedelta(seconds=timeout)
        return timeout

    def _create_temporal_call_callback(
        self,
    ) -> Callable[..., Any]:
        """Create the CONFIG_KEY_CALL callback that routes to Temporal activities."""

        def temporal_call_callback(
            func: Any,
            input: tuple[tuple[Any, ...], dict[str, Any]],
            retry_policy: Sequence[Any] | None = None,
            cache_policy: Any | None = None,
            callbacks: Any | None = None,
        ) -> TemporalTaskFuture[Any] | InlineFuture[Any]:
            """Route task calls to Temporal activities or execute inline."""
            args, kwargs = input

            # Get task identifier
            task_id = _get_task_identifier(func)
            if task_id is None:
                raise ValueError(
                    f"Cannot route task {func} to activity: not importable. "
                    "Tasks must be defined at module level (not in __main__, "
                    "not as closures or lambdas)."
                )

            # Extract task name for options lookup
            task_name = task_id.rsplit(".", 1)[-1]

            # Check execution context
            if _is_in_activity_context():
                # We're inside an activity - execute inline
                logger.debug("Executing task %s inline (in activity context)", task_id)
                return self._execute_task_inline(func, args, kwargs)

            if _is_in_workflow_context():
                # We're in workflow - schedule as activity
                logger.debug("Scheduling task %s as activity", task_id)
                return self._schedule_task_activity(task_id, task_name, args, kwargs)

            # Not in Temporal context - execute inline (for testing)
            logger.debug("Executing task %s inline (not in Temporal context)", task_id)
            return self._execute_task_inline(func, args, kwargs)

        return temporal_call_callback

    def _execute_task_inline(
        self, func: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> InlineFuture[Any]:
        """Execute a task inline (not as an activity)."""
        try:
            # Unwrap the task function if needed
            actual_func = func
            if hasattr(func, "func"):
                actual_func = func.func

            # Execute synchronously for InlineFuture
            if asyncio.iscoroutinefunction(actual_func):
                # For async functions, we need to run in the current event loop
                # This is a limitation - inline async execution in sync context
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're in an async context, create a task
                    # But InlineFuture expects immediate result...
                    # This is a design limitation for the POC
                    raise RuntimeError(
                        "Inline async task execution not fully supported. "
                        "Use workflow context for proper async handling."
                    )
                result = loop.run_until_complete(actual_func(*args, **kwargs))
            else:
                result = actual_func(*args, **kwargs)

            return InlineFuture(result=result)
        except Exception as e:
            return InlineFuture(exception=e)

    def _schedule_task_activity(
        self,
        task_id: str,
        task_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> TemporalTaskFuture[Any]:
        """Schedule a task as a Temporal activity."""
        timeout = self._get_task_timeout(task_name)

        # Create the activity input
        activity_input = TaskActivityInput(
            task_id=task_id,
            args=args,
            kwargs=kwargs,
            entrypoint_id=self._entrypoint_id,
        )

        # Schedule the activity
        handle = workflow.start_activity(
            execute_langgraph_task,
            activity_input,
            start_to_close_timeout=timeout,
        )

        # Create and return the future
        future: TemporalTaskFuture[Any] = TemporalTaskFuture(activity_handle=handle)
        return future

    async def ainvoke(
        self,
        input_state: Any,
        config: dict[str, Any] | None = None,
        on_interrupt: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        """Invoke the entrypoint asynchronously.

        Args:
            input_state: Input to pass to the entrypoint.
            config: Optional LangGraph config to merge.
            on_interrupt: Callback for handling interrupts.

        Returns:
            The result from the entrypoint execution.
        """
        from langchain_core.runnables.config import var_child_runnable_config
        from langgraph._internal._constants import (
            CONF,
            CONFIG_KEY_CALL,
        )

        pregel = self._get_pregel()

        # Create our custom call callback
        call_callback = self._create_temporal_call_callback()

        # Build config with our callback
        injected_config: dict[str, Any] = {
            "configurable": {
                CONFIG_KEY_CALL: call_callback,
            }
        }

        # Merge with user config
        if config:
            user_configurable = config.get("configurable", {})
            injected_config["configurable"] = {
                **user_configurable,
                CONFIG_KEY_CALL: call_callback,  # Our callback takes precedence
            }
            # Copy other config keys
            for key, value in config.items():
                if key != "configurable":
                    injected_config[key] = value

        # Ensure CONF key exists for LangGraph internals
        if CONF not in injected_config:
            injected_config[CONF] = injected_config["configurable"]

        # Set context var and run
        # Cast to RunnableConfig for type checking (it's a TypedDict that accepts extra keys)
        runnable_config = cast("RunnableConfig", injected_config)
        token = var_child_runnable_config.set(runnable_config)
        try:
            result = await pregel.ainvoke(input_state, runnable_config)

            # Handle interrupts if callback provided
            if isinstance(result, dict) and "__interrupt__" in result and on_interrupt:
                interrupt_value = result["__interrupt__"]
                if isinstance(interrupt_value, list) and interrupt_value:
                    # Get the first interrupt's value
                    interrupt_data = interrupt_value[0]
                    if hasattr(interrupt_data, "value"):
                        interrupt_data = interrupt_data.value

                    # Call the interrupt handler
                    resume_value = await on_interrupt(interrupt_data)

                    # Resume with Command
                    from langgraph.types import Command

                    result = await self.ainvoke(
                        Command(resume=resume_value),
                        config=config,
                        on_interrupt=on_interrupt,
                    )

            return result
        finally:
            var_child_runnable_config.reset(token)

    def get_graph(self) -> Pregel:
        """Get the underlying Pregel graph."""
        return self._get_pregel()

    def get_graph_ascii(self) -> str:
        """Get ASCII visualization of the graph."""
        try:
            return self._get_pregel().get_graph().draw_ascii()
        except Exception:
            return "Graph visualization not available"

    def get_graph_mermaid(self) -> str:
        """Get Mermaid diagram of the graph."""
        try:
            return self._get_pregel().get_graph().draw_mermaid()
        except Exception:
            return "Graph visualization not available"


def compile_functional(
    entrypoint_id: str,
    default_task_timeout: timedelta = timedelta(minutes=5),
    task_options: dict[str, dict[str, Any]] | None = None,
) -> TemporalFunctionalRunner:
    """Compile a registered entrypoint for Temporal execution.

    This is the main entry point for using functional API entrypoints
    in Temporal workflows.

    Args:
        entrypoint_id: ID of the registered entrypoint.
        default_task_timeout: Default timeout for task activities.
        task_options: Per-task activity options.

    Returns:
        A TemporalFunctionalRunner that can be used to invoke the entrypoint.

    Example:
        ```python
        @workflow.defn
        class MyWorkflow:
            @workflow.run
            async def run(self, input: str) -> dict:
                app = compile_functional("my_entrypoint")
                return await app.ainvoke(input)
        ```
    """
    return TemporalFunctionalRunner(
        entrypoint_id=entrypoint_id,
        default_task_timeout=default_task_timeout,
        task_options=task_options,
    )
