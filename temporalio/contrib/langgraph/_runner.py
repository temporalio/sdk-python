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

    Example:
        >>> from temporalio.contrib.langgraph import compile
        >>>
        >>> @workflow.defn
        >>> class MyWorkflow:
        ...     @workflow.run
        ...     async def run(self, graph_id: str, input_data: dict):
        ...         app = compile(graph_id)
        ...         return await app.ainvoke(input_data)
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

    async def ainvoke(
        self,
        input_state: dict[str, Any],
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Execute the graph asynchronously.

        This method runs the Pregel loop using AsyncPregelLoop for proper
        graph traversal, executing each node as a Temporal activity.

        Args:
            input_state: The initial state to pass to the graph.
            config: Optional configuration for the execution.

        Returns:
            The final state after graph execution.
        """
        # Import here to avoid workflow sandbox issues
        with workflow.unsafe.imports_passed_through():
            from langgraph.pregel._loop import AsyncPregelLoop
            from langgraph.pregel._io import read_channels

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
                await asyncio.gather(*[
                    self._execute_task(task, loop) for task in tasks_to_execute
                ])

                # Process writes and advance to next step
                loop.after_tick()

        # Return final output (set by loop.__aexit__)
        return cast("dict[str, Any]", loop.output)

    async def _execute_task(self, task: PregelExecutableTask, loop: Any) -> None:
        """Execute a single task, either in workflow or as activity.

        Args:
            task: The Pregel task to execute.
            loop: The AsyncPregelLoop instance for recording writes.
        """
        if self._should_run_in_workflow(task.name):
            # Execute directly in workflow (for deterministic operations)
            writes = await self._execute_in_workflow(task)
        else:
            # Execute as activity
            writes = await self._execute_as_activity(task)

        # Record writes to the loop
        # This is how activity results flow back into the Pregel state
        task.writes.extend(writes)

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
    ) -> list[tuple[str, Any]]:
        """Execute a task as a Temporal activity.

        Args:
            task: The task to execute.

        Returns:
            List of (channel, value) tuples representing the writes.
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
