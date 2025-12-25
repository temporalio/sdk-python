"""Temporal runner for LangGraph graphs.

This module provides TemporalLangGraphRunner, which wraps a compiled LangGraph
graph and executes nodes as Temporal activities for durable execution.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Optional, cast

from temporalio import workflow

from temporalio.contrib.langgraph._models import (
    ChannelWrite,
    NodeActivityInput,
    NodeActivityOutput,
)

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.pregel import Pregel


class TemporalLangGraphRunner:
    """Runner that executes LangGraph graphs with Temporal activities.

    This runner wraps a compiled LangGraph graph (Pregel) and provides
    an interface similar to the standard graph, but executes nodes as
    Temporal activities for durable execution.

    The runner:
    - Executes the Pregel loop deterministically in the workflow
    - Routes node execution to Temporal activities
    - Captures node outputs and applies them to state
    - Handles retries and timeouts via Temporal

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

        This method runs the Pregel loop, executing each node as a
        Temporal activity and collecting the results.

        Args:
            input_state: The initial state to pass to the graph.
            config: Optional configuration for the execution.

        Returns:
            The final state after graph execution.
        """
        config = config or {}

        # Initialize state with input
        state = dict(input_state)

        # Get the graph structure
        nodes = self.pregel.nodes

        # Simple execution: iterate through nodes in order
        # TODO: Full Pregel loop implementation with proper task scheduling
        for node_name, pregel_node in nodes.items():
            # Check if node should run in workflow
            if self._should_run_in_workflow(node_name):
                # Execute directly in workflow (for deterministic operations)
                result = await self._execute_in_workflow(node_name, state, config)
            else:
                # Execute as activity
                result = await self._execute_as_activity(node_name, state, config)

            # Apply writes to state
            if result:
                for channel, value in result:
                    state[channel] = value

        return state

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
        # Note: This would need to be set when the node was added to the graph
        metadata = getattr(node, "metadata", None) or {}
        temporal_config = metadata.get("temporal", {})
        return temporal_config.get("run_in_workflow", False)

    async def _execute_in_workflow(
        self,
        node_name: str,
        state: dict[str, Any],
        config: dict[str, Any],
    ) -> list[tuple[str, Any]]:
        """Execute a node directly in the workflow.

        This is used for deterministic operations that don't need
        activity durability.

        Args:
            node_name: The name of the node to execute.
            state: The current state.
            config: The configuration.

        Returns:
            List of (channel, value) tuples representing the writes.
        """
        node = self.pregel.nodes.get(node_name)
        if node is None or node.node is None:
            return []

        # Execute the node directly
        # Cast config to RunnableConfig for type checking
        runnable_config = cast("RunnableConfig", config)
        result = node.node.invoke(state, runnable_config)

        # Convert result to writes
        if isinstance(result, dict):
            return list(result.items())
        return []

    async def _execute_as_activity(
        self,
        node_name: str,
        state: dict[str, Any],
        config: dict[str, Any],
    ) -> list[tuple[str, Any]]:
        """Execute a node as a Temporal activity.

        Args:
            node_name: The name of the node to execute.
            state: The current state.
            config: The configuration.

        Returns:
            List of (channel, value) tuples representing the writes.
        """
        self._step_counter += 1

        # Build activity input
        activity_input = NodeActivityInput(
            node_name=node_name,
            task_id=f"{node_name}_{self._step_counter}_{workflow.info().workflow_id}",
            graph_id=self.graph_id,
            input_state=state,
            config=self._filter_config(config),
            path=(),
            triggers=[],
        )

        # Get node-specific configuration
        timeout = self._get_node_timeout(node_name)
        task_queue = self._get_node_task_queue(node_name)
        retry_policy = self._get_node_retry_policy(node_name)
        heartbeat_timeout = self._get_node_heartbeat_timeout(node_name)

        # Execute activity
        result: NodeActivityOutput = await workflow.execute_activity(
            "execute_langgraph_node",
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
