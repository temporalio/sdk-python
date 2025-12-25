"""Temporal runner for LangGraph graphs.

This module provides TemporalLangGraphRunner, which wraps a compiled LangGraph
graph and executes nodes as Temporal activities for durable execution.

Architecture:
    - The Pregel loop runs in the workflow (deterministic orchestration)
    - Node execution is routed to Temporal activities (non-deterministic I/O)
    - The runner uses AsyncPregelLoop for proper graph traversal and state management
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Optional, cast

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.contrib.langgraph._activities import execute_node

from temporalio.contrib.langgraph._models import (
    InterruptValue,
    NodeActivityInput,
)

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.pregel import Pregel
    from langgraph.types import PregelExecutableTask


class TemporalLangGraphRunner:
    """Runner that executes LangGraph graphs with Temporal activities.

    This runner wraps a compiled LangGraph graph (Pregel) and provides
    an interface similar to the standard graph, but executes nodes as
    Temporal activities for durable execution.

    The runner uses LangGraph's AsyncPregelLoop for proper graph orchestration:
    - Evaluates conditional edges
    - Manages state channels
    - Handles task scheduling based on graph topology
    - Routes node execution to Temporal activities

    Human-in-the-Loop Support:
        When a node calls LangGraph's interrupt() function, ainvoke() returns
        a result dict containing '__interrupt__' key with the interrupt info.
        This matches LangGraph's native API. To resume, call ainvoke() with
        Command(resume=value).

    Example (basic):
        >>> from temporalio.contrib.langgraph import compile
        >>>
        >>> @workflow.defn
        >>> class MyWorkflow:
        ...     @workflow.run
        ...     async def run(self, graph_id: str, input_data: dict):
        ...         app = compile(graph_id)
        ...         return await app.ainvoke(input_data)

    Example (with interrupts - LangGraph native API):
        >>> from temporalio.contrib.langgraph import compile
        >>> from langgraph.types import Command
        >>>
        >>> @workflow.defn
        >>> class MyWorkflow:
        ...     def __init__(self):
        ...         self._human_response = None
        ...
        ...     @workflow.signal
        ...     def provide_input(self, value: str):
        ...         self._human_response = value
        ...
        ...     @workflow.run
        ...     async def run(self, input_data: dict):
        ...         app = compile("my_graph")
        ...         result = await app.ainvoke(input_data)
        ...
        ...         # Check for interrupt (same as native LangGraph API)
        ...         if '__interrupt__' in result:
        ...             interrupt_info = result['__interrupt__'][0]
        ...             # interrupt_info.value contains data from interrupt()
        ...
        ...             # Wait for human input via signal
        ...             await workflow.wait_condition(
        ...                 lambda: self._human_response is not None
        ...             )
        ...
        ...             # Resume using LangGraph's Command API
        ...             result = await app.ainvoke(Command(resume=self._human_response))
        ...
        ...         return result
    """

    def __init__(
        self,
        pregel: Pregel,
        graph_id: str,
        default_activity_timeout: Optional[timedelta] = None,
        default_max_retries: int = 3,
        default_task_queue: Optional[str] = None,
        enable_workflow_execution: bool = False,
    ) -> None:
        """Initialize the Temporal runner.

        Args:
            pregel: The compiled Pregel graph instance.
            graph_id: The ID of the graph in the registry.
            default_activity_timeout: Default timeout for node activities.
                Defaults to 5 minutes if not specified.
            default_max_retries: Default maximum retry attempts for activities.
            default_task_queue: Default task queue for activities.
                If None, uses the workflow's task queue.
            enable_workflow_execution: If True, nodes marked with
                metadata={"temporal": {"run_in_workflow": True}} will
                execute directly in the workflow instead of as activities.
        """
        # Validate no step_timeout
        if pregel.step_timeout is not None:
            raise ValueError(
                "LangGraph's step_timeout uses time.monotonic() which is "
                "non-deterministic. Use per-node activity timeouts instead."
            )

        self.pregel = pregel
        self.graph_id = graph_id
        self.default_activity_timeout = default_activity_timeout or timedelta(minutes=5)
        self.default_max_retries = default_max_retries
        self.default_task_queue = default_task_queue
        self.enable_workflow_execution = enable_workflow_execution
        self._step_counter = 0
        # State for interrupt handling
        self._interrupted_state: Optional[dict[str, Any]] = None
        self._interrupted_node_name: Optional[str] = None  # Track which node interrupted
        self._resume_value: Optional[Any] = None
        self._resume_used: bool = False
        # Pending interrupt from current execution (set by _execute_as_activity)
        self._pending_interrupt: Optional[InterruptValue] = None

    async def ainvoke(
        self,
        input_state: dict[str, Any] | Any,
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Execute the graph asynchronously.

        This method runs the Pregel loop using AsyncPregelLoop for proper
        graph traversal, executing each node as a Temporal activity.

        Args:
            input_state: The initial state to pass to the graph, OR a
                Command(resume=value) to resume after an interrupt.
                When resuming with Command, the state from the previous
                interrupt will be used.
            config: Optional configuration for the execution.

        Returns:
            The final state after graph execution. If a node called
            interrupt(), the result will contain '__interrupt__' key
            with a list of Interrupt objects (matching LangGraph's
            native API).

        Example (basic):
            >>> result = await app.ainvoke({"messages": [HumanMessage(content="Hi")]})

        Example (handling interrupt - LangGraph native API):
            >>> from langgraph.types import Command
            >>>
            >>> result = await app.ainvoke(initial_state)
            >>> if '__interrupt__' in result:
            ...     # result['__interrupt__'][0].value has the interrupt data
            ...     # Get human input...
            ...     result = await app.ainvoke(Command(resume=human_input))
        """
        # Import Command here to check type
        with workflow.unsafe.imports_passed_through():
            from langgraph.types import Command

        # Track resume state for this invocation
        resume_value: Optional[Any] = None

        # Check if input is a Command with resume value (LangGraph API)
        if isinstance(input_state, Command):
            if hasattr(input_state, "resume") and input_state.resume is not None:
                resume_value = input_state.resume
            # When resuming, use the state from the last interrupt
            if self._interrupted_state is None:
                raise ValueError(
                    "Cannot resume with Command - no previous interrupt state. "
                    "Call ainvoke() first and check for '__interrupt__' in the result."
                )
            input_state = self._interrupted_state

        self._resume_value = resume_value
        self._resume_used = False
        # Reset pending interrupt for this invocation
        self._pending_interrupt = None

        # Import here to avoid workflow sandbox issues
        with workflow.unsafe.imports_passed_through():
            from langgraph.pregel._loop import AsyncPregelLoop
            from langgraph.pregel._io import read_channels
            from langgraph.types import Interrupt

        config = config or {}

        # Ensure config has required structure
        if "configurable" not in config:
            config["configurable"] = {}
        if "recursion_limit" not in config:
            config["recursion_limit"] = 25

        # Create AsyncPregelLoop with all required parameters
        # Cast config to RunnableConfig for type checking
        loop = AsyncPregelLoop(
            input=input_state,
            stream=None,  # No streaming for now
            config=cast("RunnableConfig", config),
            store=getattr(self.pregel, "store", None),
            cache=getattr(self.pregel, "cache", None),
            checkpointer=None,  # Use Temporal's event history instead
            nodes=self.pregel.nodes,
            specs=self.pregel.channels,
            trigger_to_nodes=getattr(self.pregel, "trigger_to_nodes", {}),
            durability="sync",  # Temporal handles durability
            input_keys=getattr(self.pregel, "input_channels", None) or [],
            output_keys=getattr(self.pregel, "output_channels", None) or [],
            stream_keys=getattr(self.pregel, "stream_channels_asis", None) or [],
        )

        # Use direct async with to ensure __aexit__ sets loop.output
        async with loop:
            # Execute the Pregel loop
            # loop.tick() prepares the next tasks based on graph topology
            # We execute tasks and call loop.after_tick() to process writes
            while loop.tick():
                # Get tasks that need to be executed (those without writes)
                tasks_to_execute = [
                    task for task in loop.tasks.values() if not task.writes
                ]

                # Execute all tasks in parallel (BSP model allows parallelism
                # within a tick, we just need to wait for all before after_tick)
                # Collect results to check for interrupts
                results = await asyncio.gather(*[
                    self._execute_task(task, loop) for task in tasks_to_execute
                ])

                # Check if any task was interrupted
                if not all(results):
                    # An interrupt occurred - break the loop
                    break

                # Process writes and advance to next step
                loop.after_tick()

        # Get the output from the loop
        output = cast("dict[str, Any]", loop.output) if loop.output else {}

        # If there's a pending interrupt, add it to the result (LangGraph native API)
        if self._pending_interrupt is not None:
            # Create LangGraph Interrupt object to match native API
            interrupt_obj = Interrupt.from_ns(
                value=self._pending_interrupt.value,
                ns="",  # Empty namespace since we don't use checkpointing
            )
            # Merge with any existing state in output
            output = {**output, "__interrupt__": [interrupt_obj]}

        return output

    async def _execute_task(self, task: PregelExecutableTask, loop: Any) -> bool:
        """Execute a single task, either in workflow or as activity.

        Args:
            task: The Pregel task to execute.
            loop: The AsyncPregelLoop instance for recording writes.

        Returns:
            True if execution should continue, False if an interrupt occurred.
        """
        # Determine if this task should receive the resume value
        # Only pass resume value to the specific node that was interrupted
        resume_for_task = None
        if (
            self._resume_value is not None
            and not self._resume_used
            and self._interrupted_node_name == task.name
        ):
            # This is the node that was interrupted - pass the resume value
            resume_for_task = self._resume_value

        if self._should_run_in_workflow(task.name):
            # Execute directly in workflow (for deterministic operations)
            # Note: workflow execution doesn't support interrupts currently
            writes = await self._execute_in_workflow(task)
        else:
            # Execute as activity
            writes = await self._execute_as_activity(task, resume_for_task)

        # Check if an interrupt occurred
        if self._pending_interrupt is not None:
            # The task interrupted - don't mark resume as used
            return False

        # If we provided a resume value and the task completed successfully,
        # it means the task consumed the resume value (interrupt() returned it)
        if resume_for_task is not None:
            self._resume_used = True

        # Record writes to the loop
        # This is how activity results flow back into the Pregel state
        task.writes.extend(writes)
        return True

    def _should_run_in_workflow(self, node_name: str) -> bool:
        """Check if a node should run directly in the workflow.

        Args:
            node_name: The name of the node.

        Returns:
            True if the node should run in workflow, False for activity.
        """
        if not self.enable_workflow_execution:
            return False

        # Check node metadata
        node = self.pregel.nodes.get(node_name)
        if node is None:
            return False

        # Look for temporal.run_in_workflow in metadata
        metadata = getattr(node, "metadata", None) or {}
        temporal_config = metadata.get("temporal", {})
        return temporal_config.get("run_in_workflow", False)

    async def _execute_in_workflow(
        self,
        task: PregelExecutableTask,
    ) -> list[tuple[str, Any]]:
        """Execute a task directly in the workflow.

        This is used for deterministic operations that don't need
        activity durability.

        Args:
            task: The task to execute.

        Returns:
            List of (channel, value) tuples representing the writes.
        """
        with workflow.unsafe.imports_passed_through():
            from collections import deque
            from langgraph.constants import CONFIG_KEY_SEND

        # Setup write capture
        writes: deque[tuple[str, Any]] = deque()

        # Inject write callback into config
        config = {
            **task.config,
            "configurable": {
                **task.config.get("configurable", {}),
                CONFIG_KEY_SEND: writes.extend,
            },
        }

        # Execute the task's proc (the node's runnable)
        if task.proc is not None:
            runnable_config = cast("RunnableConfig", config)
            if asyncio.iscoroutinefunction(getattr(task.proc, "ainvoke", None)):
                await task.proc.ainvoke(task.input, runnable_config)
            else:
                task.proc.invoke(task.input, runnable_config)

        return list(writes)

    async def _execute_as_activity(
        self,
        task: PregelExecutableTask,
        resume_value: Optional[Any] = None,
    ) -> list[tuple[str, Any]]:
        """Execute a task as a Temporal activity.

        Args:
            task: The task to execute.
            resume_value: If provided, passed to the activity to resume
                an interrupted node. The node's interrupt() call will
                return this value instead of raising.

        Returns:
            List of (channel, value) tuples representing the writes.
            If the node called interrupt(), _pending_interrupt will be set.
        """
        self._step_counter += 1

        # Build activity input
        activity_input = NodeActivityInput(
            node_name=task.name,
            task_id=task.id,
            graph_id=self.graph_id,
            input_state=task.input,
            config=self._filter_config(cast("dict[str, Any]", task.config)),
            path=cast("tuple[str | int, ...]", task.path),
            triggers=list(task.triggers) if task.triggers else [],
            resume_value=resume_value,
        )

        # Get node-specific configuration
        timeout = self._get_node_timeout(task.name)
        task_queue = self._get_node_task_queue(task.name)
        retry_policy = self._get_node_retry_policy(task.name)
        heartbeat_timeout = self._get_node_heartbeat_timeout(task.name)

        # Execute activity
        result = await workflow.execute_activity(
            execute_node,
            activity_input,
            start_to_close_timeout=timeout,
            task_queue=task_queue,
            retry_policy=retry_policy,
            heartbeat_timeout=heartbeat_timeout,
        )

        # Check if the node raised an interrupt
        if result.interrupt is not None:
            # Save state for resume - use task input as the state at interrupt
            self._interrupted_state = cast("dict[str, Any]", task.input)
            # Save which node interrupted so we can pass resume value to it
            self._interrupted_node_name = task.name
            # Store the interrupt for the caller to handle
            self._pending_interrupt = result.interrupt
            # Return empty writes - the interrupt stops further execution
            return []

        # Convert ChannelWrite objects to tuples
        return result.to_write_tuples()

    def _filter_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Filter configuration for serialization.

        Removes internal LangGraph keys that shouldn't be serialized.

        Args:
            config: The original configuration.

        Returns:
            Filtered configuration safe for serialization.
        """
        # Keys to exclude from serialization
        exclude_prefixes = ("__pregel_", "__lg_")

        filtered: dict[str, Any] = {}
        for key, value in config.items():
            if not any(key.startswith(prefix) for prefix in exclude_prefixes):
                if key == "configurable" and isinstance(value, dict):
                    # Also filter configurable dict
                    filtered[key] = {
                        k: v
                        for k, v in value.items()
                        if not any(k.startswith(prefix) for prefix in exclude_prefixes)
                    }
                else:
                    filtered[key] = value

        return filtered

    def _get_node_metadata(self, node_name: str) -> dict[str, Any]:
        """Get Temporal-specific metadata for a node.

        Args:
            node_name: The name of the node.

        Returns:
            Dict with temporal config from node.metadata.get("temporal", {})
        """
        node = self.pregel.nodes.get(node_name)
        if node is None:
            return {}
        metadata = getattr(node, "metadata", None) or {}
        return metadata.get("temporal", {})

    def _get_node_timeout(self, node_name: str) -> timedelta:
        """Get the timeout for a specific node.

        Priority: node metadata > default
        Looks for metadata={"temporal": {"activity_timeout": timedelta(...)}}

        Args:
            node_name: The name of the node.

        Returns:
            The timeout for the node's activity.
        """
        temporal_config = self._get_node_metadata(node_name)
        timeout = temporal_config.get("activity_timeout")
        if isinstance(timeout, timedelta):
            return timeout
        return self.default_activity_timeout

    def _get_node_task_queue(self, node_name: str) -> Optional[str]:
        """Get the task queue for a specific node.

        Priority: node metadata > default
        Looks for metadata={"temporal": {"task_queue": "queue-name"}}

        Args:
            node_name: The name of the node.

        Returns:
            The task queue for the node's activity, or None for default.
        """
        temporal_config = self._get_node_metadata(node_name)
        task_queue = temporal_config.get("task_queue")
        if isinstance(task_queue, str):
            return task_queue
        return self.default_task_queue

    def _get_node_heartbeat_timeout(self, node_name: str) -> Optional[timedelta]:
        """Get the heartbeat timeout for a specific node.

        Looks for metadata={"temporal": {"heartbeat_timeout": timedelta(...)}}

        Args:
            node_name: The name of the node.

        Returns:
            The heartbeat timeout, or None if not specified.
        """
        temporal_config = self._get_node_metadata(node_name)
        timeout = temporal_config.get("heartbeat_timeout")
        if isinstance(timeout, timedelta):
            return timeout
        return None

    def _get_node_retry_policy(self, node_name: str) -> Any:
        """Get the retry policy for a specific node.

        Maps LangGraph's RetryPolicy to Temporal's RetryPolicy.
        Priority: node retry_policy > default

        LangGraph RetryPolicy fields:
        - initial_interval: float (seconds)
        - backoff_factor: float
        - max_interval: float (seconds)
        - max_attempts: int
        - jitter: bool (not mapped to Temporal)
        - retry_on: Callable (not mapped to Temporal)

        Args:
            node_name: The name of the node.

        Returns:
            Temporal RetryPolicy for the node's activity.
        """
        from temporalio.common import RetryPolicy

        node = self.pregel.nodes.get(node_name)
        if node is None:
            return RetryPolicy(maximum_attempts=self.default_max_retries)

        # Check for LangGraph retry_policy
        retry_policies = getattr(node, "retry_policy", None)
        if retry_policies and len(retry_policies) > 0:
            # LangGraph stores as tuple, use first policy
            lg_policy = retry_policies[0]
            return RetryPolicy(
                initial_interval=timedelta(seconds=lg_policy.initial_interval),
                backoff_coefficient=lg_policy.backoff_factor,
                maximum_interval=timedelta(seconds=lg_policy.max_interval),
                maximum_attempts=lg_policy.max_attempts,
            )

        return RetryPolicy(maximum_attempts=self.default_max_retries)

    def invoke(
        self,
        input_state: dict[str, Any],
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Synchronous invoke is not supported in Temporal workflows.

        Use ainvoke() instead.

        Raises:
            NotImplementedError: Always raised.
        """
        raise NotImplementedError(
            "Synchronous invoke() is not supported in Temporal workflows. "
            "Use ainvoke() instead."
        )
