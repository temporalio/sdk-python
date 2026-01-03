"""Runner for LangGraph functional API with Temporal integration.

This module provides the TemporalFunctionalRunner that executes LangGraph
@entrypoint functions directly (bypassing Pregel's runner) with CONFIG_KEY_CALL
injected to route @task calls to Temporal activities.

LangGraph Internal API Usage
============================

This module uses LangGraph internal APIs because we execute the entrypoint
function directly, bypassing Pregel's normal execution loop. This allows us
to inject our own CONFIG_KEY_CALL callback that routes @task calls to
Temporal activities.

WHY WE BYPASS PREGEL'S RUNNER:
Pregel's runner always overwrites CONFIG_KEY_CALL with its own implementation.
By extracting and executing the entrypoint function directly, we can inject
our callback and have @task calls routed to Temporal activities.

CONTEXT KEYS WE INJECT:
- CONFIG_KEY_CALL: Our callback that routes @task to activities
- CONFIG_KEY_SCRATCHPAD: For interrupt() support
- CONFIG_KEY_RUNTIME: For get_store()/get_stream_writer() support
- CONFIG_KEY_CHECKPOINT_NS: Namespace for checkpoint operations

RISKS:
These are private APIs that may change in future LangGraph versions.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
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


@dataclass
class FunctionalExecutionState:
    """Tracks state during functional API entrypoint execution."""

    pending_interrupt: Any | None = None
    """Pending interrupt value from interrupt() call."""

    pending_parent_command: Any | None = None
    """Pending parent command from subgraph (for parent graph routing)."""

    resume_value: Any | None = None
    """Value to resume with after interrupt."""


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

    def _get_task_options(self, task_name: str) -> dict[str, Any]:
        """Get activity options for a specific task.

        Supports both raw options and activity_options() format:
        - Raw: {"start_to_close_timeout": timedelta(...)}
        - activity_options(): {"temporal": {"start_to_close_timeout": ...}}
        """
        task_opts = self._task_options.get(task_name, {})
        # Unwrap activity_options() format if present
        if "temporal" in task_opts:
            return task_opts["temporal"]
        return task_opts

    def _get_task_timeout(self, task_name: str) -> timedelta:
        """Get the timeout for a specific task."""
        task_opts = self._get_task_options(task_name)
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

    def _extract_entrypoint_function(self, pregel: Pregel) -> Callable[..., Any]:
        """Extract the underlying entrypoint function from the Pregel wrapper.

        The @entrypoint decorator creates a single-node Pregel graph where
        the node contains the user's function wrapped in a RunnableCallable.

        Returns:
            The underlying async or sync entrypoint function.
        """
        # Get the single node from the entrypoint Pregel
        if len(pregel.nodes) != 1:
            raise ValueError(
                f"Expected single-node Pregel from @entrypoint, got {len(pregel.nodes)} nodes"
            )

        node_name = next(iter(pregel.nodes.keys()))
        node = pregel.nodes[node_name]
        bound = node.bound

        # Extract the function from RunnableCallable
        # For async functions: afunc is the actual function
        # For sync functions: func is the actual function, afunc is run_in_executor wrapper
        afunc = getattr(bound, "afunc", None)
        func = getattr(bound, "func", None)

        if afunc is not None:
            # Check if afunc is a partial (sync function wrapped in run_in_executor)
            if isinstance(afunc, functools.partial):
                # This is a sync function - use func directly
                if func is not None:
                    return func
            else:
                # This is an async function
                return afunc

        if func is not None:
            return func

        raise ValueError(f"Cannot extract function from entrypoint node {node_name}")

    def _build_execution_config(
        self,
        call_callback: Callable[..., Any],
        user_config: dict[str, Any] | None,
        execution_state: FunctionalExecutionState,
    ) -> dict[str, Any]:
        """Build the config dict with all required context keys injected.

        This injects:
        - CONFIG_KEY_CALL: Our callback for routing @task to activities
        - CONFIG_KEY_SCRATCHPAD: For interrupt() support
        - CONFIG_KEY_RUNTIME: For get_store()/get_stream_writer()
        - CONFIG_KEY_CHECKPOINT_NS: Namespace for checkpoints
        """
        from langgraph._internal._constants import (
            CONF,
            CONFIG_KEY_CALL,
            CONFIG_KEY_CHECKPOINT_NS,
            CONFIG_KEY_RUNTIME,
            CONFIG_KEY_SCRATCHPAD,
        )
        from langgraph._internal._scratchpad import PregelScratchpad
        from langgraph.runtime import Runtime

        # Create scratchpad for interrupt handling
        # Track interrupt index using a mutable container
        interrupt_idx = [0]

        def _interrupt_counter() -> int:
            idx = interrupt_idx[0]
            interrupt_idx[0] += 1
            return idx

        def _get_null_resume(consume: bool) -> Any:
            return None

        # Set up resume values if resuming from interrupt
        resume_values: list[Any] = []
        if execution_state.resume_value is not None:
            resume_values = [execution_state.resume_value]

        scratchpad = PregelScratchpad(
            step=0,
            stop=1,
            call_counter=lambda: 0,
            interrupt_counter=_interrupt_counter,
            get_null_resume=_get_null_resume,
            resume=resume_values,
            subgraph_counter=lambda: 0,
        )

        # Create runtime (without store for now - can add later)
        runtime = Runtime(store=None)

        # Build configurable dict with all context keys
        configurable: dict[str, Any] = {
            CONFIG_KEY_CALL: call_callback,
            CONFIG_KEY_SCRATCHPAD: scratchpad,
            CONFIG_KEY_RUNTIME: runtime,
            CONFIG_KEY_CHECKPOINT_NS: "",
        }

        # Merge with user config
        if user_config:
            user_configurable = user_config.get("configurable", {})
            # User config goes first, our keys take precedence
            configurable = {
                **user_configurable,
                **configurable,
            }

        # Build full config
        injected_config: dict[str, Any] = {
            "configurable": configurable,
            CONF: configurable,  # LangGraph internals use CONF key
            "callbacks": None,  # Required by call() in langgraph/pregel/_call.py
        }

        # Copy other user config keys
        if user_config:
            for key, value in user_config.items():
                if key not in ("configurable", CONF):
                    injected_config[key] = value

        return injected_config

    async def ainvoke(
        self,
        input_state: Any,
        config: dict[str, Any] | None = None,
        on_interrupt: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        """Invoke the entrypoint asynchronously.

        This executes the entrypoint function directly (bypassing Pregel's runner)
        with our CONFIG_KEY_CALL injected, so @task calls are routed to activities.

        Args:
            input_state: Input to pass to the entrypoint.
            config: Optional LangGraph config to merge.
            on_interrupt: Callback for handling interrupts.

        Returns:
            The result from the entrypoint execution.
        """
        from langchain_core.runnables.config import var_child_runnable_config
        from langgraph.errors import GraphInterrupt as LangGraphInterrupt
        from langgraph.errors import ParentCommand

        pregel = self._get_pregel()
        execution_state = FunctionalExecutionState()

        # Extract the underlying entrypoint function
        entrypoint_func = self._extract_entrypoint_function(pregel)

        # Create our custom call callback
        call_callback = self._create_temporal_call_callback()

        # Build config with all required context keys
        injected_config = self._build_execution_config(
            call_callback, config, execution_state
        )

        # Set context var so get_config() works inside the entrypoint
        runnable_config = cast("RunnableConfig", injected_config)
        token = var_child_runnable_config.set(runnable_config)

        try:
            # Execute the entrypoint function directly
            if asyncio.iscoroutinefunction(entrypoint_func):
                result = await entrypoint_func(input_state)
            else:
                # For sync functions, run in the workflow context
                # Note: This runs synchronously which is fine for workflow code
                result = entrypoint_func(input_state)

            # Wrap result in dict if needed (entrypoint returns are typically dicts)
            if not isinstance(result, dict):
                result = {"result": result}

            return result

        except LangGraphInterrupt as e:
            # Handle interrupt() calls from within the entrypoint
            logger.debug("Entrypoint %s raised interrupt", self._entrypoint_id)

            # Extract interrupt value
            interrupt_value = None
            if e.args and len(e.args) > 0:
                interrupts = e.args[0]
                if interrupts and len(interrupts) > 0:
                    interrupt_value = interrupts[0].value

            execution_state.pending_interrupt = interrupt_value

            # If callback provided, handle the interrupt
            if on_interrupt and interrupt_value is not None:
                resume_value = await on_interrupt(interrupt_value)

                # Resume execution with the resume value
                execution_state.resume_value = resume_value
                execution_state.pending_interrupt = None

                # Rebuild config with resume value
                injected_config = self._build_execution_config(
                    call_callback, config, execution_state
                )
                runnable_config = cast("RunnableConfig", injected_config)

                # Reset context var for resumed execution
                var_child_runnable_config.reset(token)
                token = var_child_runnable_config.set(runnable_config)

                # Re-execute with resume value
                if asyncio.iscoroutinefunction(entrypoint_func):
                    result = await entrypoint_func(input_state)
                else:
                    result = entrypoint_func(input_state)

                if not isinstance(result, dict):
                    result = {"result": result}
                return result

            # Return interrupt marker for caller to handle
            return {"__interrupt__": [{"value": interrupt_value}]}

        except ParentCommand as e:
            # Handle commands from subgraphs back to parent
            logger.debug("Entrypoint %s raised ParentCommand", self._entrypoint_id)

            command = e.args[0] if e.args else None
            execution_state.pending_parent_command = command

            # Extract any state updates from the command
            command_result: dict[str, Any] = {}
            if command and hasattr(command, "update") and command.update:
                command_result.update(command.update)

            # Add marker for parent to handle routing
            if command and hasattr(command, "goto") and command.goto:
                command_result["__parent_command__"] = {
                    "goto": command.goto,
                    "graph": getattr(command, "graph", None),
                }

            return command_result

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
    """
    return TemporalFunctionalRunner(
        entrypoint_id=entrypoint_id,
        default_task_timeout=default_task_timeout,
        task_options=task_options,
    )
