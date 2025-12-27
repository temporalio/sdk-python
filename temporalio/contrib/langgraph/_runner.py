"""Temporal runner for LangGraph graphs."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable, Optional, cast

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.contrib.langgraph._activities import (
        langgraph_node,
        resume_langgraph_node,
    )

from temporalio.contrib.langgraph._models import (
    InterruptValue,
    NodeActivityInput,
    StateSnapshot,
    StoreItem,
    StoreSnapshot,
    StoreWrite,
)

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.pregel import Pregel
    from langgraph.types import PregelExecutableTask


class TemporalLangGraphRunner:
    """Runner that executes LangGraph graphs with Temporal activities.

    Wraps a compiled Pregel graph and executes nodes as Temporal activities.
    Uses AsyncPregelLoop for graph orchestration. Supports interrupts via
    LangGraph's native API (``__interrupt__`` key and ``Command(resume=...)``).
    """

    def __init__(
        self,
        pregel: Pregel,
        graph_id: str,
        default_activity_options: Optional[dict[str, Any]] = None,
        per_node_activity_options: Optional[dict[str, dict[str, Any]]] = None,
        checkpoint: Optional[dict[str, Any]] = None,
    ) -> None:
        """Initialize the Temporal runner.

        Args:
            pregel: The compiled Pregel graph instance.
            graph_id: The ID of the graph in the registry.
            default_activity_options: Default options for all nodes.
            per_node_activity_options: Per-node options by node name.
            checkpoint: Checkpoint from previous get_state() for continue-as-new.
        """
        # Validate no step_timeout
        if pregel.step_timeout is not None:
            raise ValueError(
                "LangGraph's step_timeout uses time.monotonic() which is "
                "non-deterministic. Use per-node activity timeouts instead."
            )

        self.pregel = pregel
        self.graph_id = graph_id
        # Extract defaults from activity_options() format
        self.default_activity_options = (default_activity_options or {}).get(
            "temporal", {}
        )
        # Extract per_node_activity_options from activity_options() format for each node
        self.per_node_activity_options = {
            node_name: cfg.get("temporal", {})
            for node_name, cfg in (per_node_activity_options or {}).items()
        }
        self._step_counter = 0
        # Track invocation number for unique activity IDs across replays
        self._invocation_counter = 0
        # State for interrupt handling
        self._interrupted_state: Optional[dict[str, Any]] = None
        self._interrupted_node_name: Optional[str] = (
            None  # Track which node interrupted
        )
        self._resume_value: Optional[Any] = None
        self._resume_used: bool = False
        # Pending interrupt from current execution (set by _execute_as_activity)
        self._pending_interrupt: Optional[InterruptValue] = None
        # Track nodes completed in current resume cycle (to avoid re-execution)
        self._completed_nodes_in_cycle: set[str] = set()
        # Cached writes from resumed nodes (injected into tasks to trigger successors)
        self._resumed_node_writes: dict[str, list[tuple[str, Any]]] = {}
        # Track the last output state for get_state()
        self._last_output: Optional[dict[str, Any]] = None
        # Store state for cross-node persistence (key: (namespace, key), value: dict)
        self._store_state: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}

        # Restore from checkpoint if provided
        if checkpoint is not None:
            self._restore_from_checkpoint(checkpoint)

    async def ainvoke(
        self,
        input_state: dict[str, Any] | Any,
        config: Optional[dict[str, Any]] = None,
        *,
        should_continue: Optional[Callable[[], bool]] = None,
    ) -> dict[str, Any]:
        """Execute the graph asynchronously.

        Args:
            input_state: Initial state or ``Command(resume=value)`` to resume.
            config: Optional configuration for the execution.
            should_continue: Callable returning False to stop for checkpointing.

        Returns:
            Final state. May contain ``__interrupt__`` or ``__checkpoint__`` keys.
        """
        workflow.logger.debug("Starting graph execution for %s", self.graph_id)

        # Import Command here to check type
        with workflow.unsafe.imports_passed_through():
            from langgraph.types import Command

        # Track resume state for this invocation
        resume_value: Optional[Any] = None

        # Check if input is a Command with resume value (LangGraph API)
        is_resume = False
        if isinstance(input_state, Command):
            is_resume = True
            if hasattr(input_state, "resume") and input_state.resume is not None:
                resume_value = input_state.resume
            # When resuming, use the state from the last interrupt
            if self._interrupted_state is None:
                raise ValueError(
                    "Cannot resume with Command - no previous interrupt state. "
                    "Call ainvoke() first and check for '__interrupt__' in the result."
                )
            input_state = self._interrupted_state
        else:
            # Fresh invocation - clear completed nodes tracking
            self._completed_nodes_in_cycle.clear()

        self._resume_value = resume_value
        self._resume_used = False
        # Reset pending interrupt for this invocation
        self._pending_interrupt = None
        # Increment invocation counter for unique activity IDs
        self._invocation_counter += 1
        # Reset step counter for this invocation
        self._step_counter = 0

        # Import here to avoid workflow sandbox issues
        with workflow.unsafe.imports_passed_through():
            from langgraph.pregel._loop import AsyncPregelLoop
            from langgraph.types import Interrupt

        config = config or {}

        # Ensure config has required structure
        if "configurable" not in config:
            config["configurable"] = {}
        if "recursion_limit" not in config:
            config["recursion_limit"] = 25

        # Handle resume case: execute the interrupted node first and cache its writes
        # The cached writes will be injected when the loop schedules this node,
        # allowing the trigger mechanism to work for successor nodes
        if is_resume and self._interrupted_node_name:
            interrupted_node = self._interrupted_node_name
            resume_writes = await self._execute_resumed_node(
                interrupted_node, input_state, config
            )
            if self._pending_interrupt is not None:
                # Node interrupted again - return immediately
                interrupt_obj = Interrupt.from_ns(
                    value=self._pending_interrupt.value,
                    ns="",
                )
                return {**input_state, "__interrupt__": [interrupt_obj]}

            # Merge the resumed node's writes into input_state
            # This ensures the writes are part of the final output even if the loop
            # doesn't schedule the resumed node (e.g., when it's the last node)
            for channel, value in resume_writes:
                input_state[channel] = value

            # Cache the writes for the trigger mechanism
            self._resumed_node_writes[interrupted_node] = resume_writes
            # ADD the resumed node to completed nodes (don't reset!)
            # This preserves knowledge of previously completed nodes across invocations,
            # preventing them from re-running when the graph continues.
            # We do need __start__ to run again to trigger the graph traversal,
            # but step1 (and other completed user nodes) should be skipped.
            # Remove __start__ from completed to allow it to run again.
            self._completed_nodes_in_cycle.discard("__start__")
            # Add the interrupted node to completed (it just ran via _execute_resumed_node)
            self._completed_nodes_in_cycle.add(interrupted_node)
            # Clear interrupted node since we've handled it
            self._interrupted_node_name = None

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

        # Execute the Pregel loop manually (not using async with to avoid blocking)
        # Enter the loop context
        await loop.__aenter__()
        interrupted = False
        try:
            # loop.tick() prepares the next tasks based on graph topology
            # We execute tasks and call loop.after_tick() to process writes
            while loop.tick():
                # Inject cached writes for resumed nodes
                # This allows the trigger mechanism to schedule successor nodes
                for task in loop.tasks.values():
                    if task.name in self._resumed_node_writes:
                        cached_writes = self._resumed_node_writes.pop(task.name)
                        task.writes.extend(cached_writes)

                # Get tasks that need to be executed (those without writes)
                # Also skip nodes that already completed in this resume cycle
                # (prevents re-execution when resuming from interrupted state)
                tasks_to_execute = [
                    task
                    for task in loop.tasks.values()
                    if not task.writes
                    and task.name not in self._completed_nodes_in_cycle
                ]

                # If no tasks to execute (all filtered out or have cached writes),
                # process any pending writes and continue to next tick
                if not tasks_to_execute:
                    loop.after_tick()
                    # Check if we should stop for checkpointing
                    if should_continue is not None and not should_continue():
                        output = (
                            cast("dict[str, Any]", loop.output) if loop.output else {}
                        )
                        output["__checkpoint__"] = self.get_state()
                        self._last_output = output
                        return output
                    continue

                # Execute tasks sequentially for now (simplifies interrupt handling)
                # TODO: Re-enable parallel execution with proper interrupt handling
                task_interrupted = False
                for task in tasks_to_execute:
                    result = await self._execute_task(task, loop)
                    if not result:
                        task_interrupted = True
                        break

                # Check if any task was interrupted
                if task_interrupted:
                    # An interrupt occurred - finalize writes before breaking
                    loop.after_tick()
                    interrupted = True
                    break

                # Process writes and advance to next step
                loop.after_tick()

                # Check if we should stop for checkpointing
                if should_continue is not None and not should_continue():
                    output = cast("dict[str, Any]", loop.output) if loop.output else {}
                    output["__checkpoint__"] = self.get_state()
                    self._last_output = output
                    return output
        finally:
            # Exit the loop context only if we completed normally (not interrupted)
            # Calling __aexit__ on interrupted loop may block indefinitely
            if not interrupted:
                await loop.__aexit__(None, None, None)

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

        # Track last output for get_state() checkpoint
        self._last_output = output

        if "__interrupt__" in output:
            workflow.logger.debug("Graph %s execution paused at interrupt", self.graph_id)
        elif "__checkpoint__" in output:
            workflow.logger.debug("Graph %s execution stopped for checkpoint", self.graph_id)
        else:
            workflow.logger.debug("Graph %s execution completed", self.graph_id)

        return output

    async def _execute_task(self, task: PregelExecutableTask, loop: Any) -> bool:
        """Execute a single task. Returns False if interrupted."""
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
            writes: list[tuple[str, Any]] = await self._execute_in_workflow(task)
            send_packets: list[Any] = []
        else:
            # Execute as activity
            writes, send_packets = await self._execute_as_activity_with_sends(
                task, resume_for_task
            )

        # Check if an interrupt occurred
        if self._pending_interrupt is not None:
            # The task interrupted - don't mark resume as used
            return False

        # Task completed successfully - track it to prevent re-execution
        self._completed_nodes_in_cycle.add(task.name)

        # If we provided a resume value and the task completed successfully,
        # it means the task consumed the resume value (interrupt() returned it)
        if resume_for_task is not None:
            self._resume_used = True

        # Record writes to the loop
        # This is how activity results flow back into the Pregel state
        task.writes.extend(writes)

        # Handle Send packets - execute each as a separate task
        # Send creates dynamic tasks with custom input (Send.arg)
        if send_packets:
            send_writes = await self._execute_send_packets(send_packets, task.config)
            if self._pending_interrupt is not None:
                return False
            task.writes.extend(send_writes)

        return True

    def _should_run_in_workflow(self, node_name: str) -> bool:
        """Check if a node should run directly in the workflow."""
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
        """Execute a task directly in the workflow for deterministic operations."""
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

    async def _execute_as_activity_with_sends(
        self,
        task: PregelExecutableTask,
        resume_value: Optional[Any] = None,
    ) -> tuple[list[tuple[str, Any]], list[Any]]:
        """Execute a task as a Temporal activity, returning writes and send packets."""
        self._step_counter += 1

        # Prepare store snapshot for the activity
        store_snapshot = self._prepare_store_snapshot()

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
            store_snapshot=store_snapshot,
        )

        # Get node-specific configuration
        activity_options = self._get_node_activity_options(task.name)

        # Generate unique activity ID
        config_dict = cast("dict[str, Any]", task.config)
        invocation_id = config_dict.get("configurable", {}).get(
            "invocation_id", self._invocation_counter
        )
        activity_id = f"inv{invocation_id}-{task.name}-{self._step_counter}"

        # Execute activity
        result = await workflow.execute_activity(
            langgraph_node,
            activity_input,
            activity_id=activity_id,
            summary=task.name,
            **activity_options,
        )

        # Apply store writes from the activity (before checking interrupt)
        if result.store_writes:
            self._apply_store_writes(result.store_writes)

        # Check if the node raised an interrupt
        if result.interrupt is not None:
            self._interrupted_state = cast("dict[str, Any]", task.input)
            self._interrupted_node_name = task.name
            self._pending_interrupt = result.interrupt
            return [], []

        # Return writes and send_packets separately
        return result.to_write_tuples(), list(result.send_packets)

    async def _execute_send_packets(
        self,
        send_packets: list[Any],
        config: Any,
    ) -> list[tuple[str, Any]]:
        """Execute Send packets as separate activities."""
        all_writes: list[tuple[str, Any]] = []

        for packet in send_packets:
            self._step_counter += 1

            # Prepare store snapshot
            store_snapshot = self._prepare_store_snapshot()

            # Build activity input with Send.arg as the input state
            activity_input = NodeActivityInput(
                node_name=packet.node,
                task_id=f"send-{packet.node}-{self._step_counter}",
                graph_id=self.graph_id,
                input_state=packet.arg,  # Send.arg is the custom input
                config=self._filter_config(cast("dict[str, Any]", config)),
                path=tuple(),
                triggers=[],
                resume_value=None,
                store_snapshot=store_snapshot,
            )

            # Get node-specific configuration
            activity_options = self._get_node_activity_options(packet.node)

            # Generate unique activity ID
            config_dict = cast("dict[str, Any]", config)
            invocation_id = config_dict.get("configurable", {}).get(
                "invocation_id", self._invocation_counter
            )
            activity_id = f"inv{invocation_id}-send-{packet.node}-{self._step_counter}"

            # Execute activity
            result = await workflow.execute_activity(
                langgraph_node,
                activity_input,
                activity_id=activity_id,
                summary=packet.node,
                **activity_options,
            )

            # Apply store writes
            if result.store_writes:
                self._apply_store_writes(result.store_writes)

            # Check for interrupt
            if result.interrupt is not None:
                self._interrupted_state = packet.arg
                self._interrupted_node_name = packet.node
                self._pending_interrupt = result.interrupt
                return all_writes

            # Collect writes
            all_writes.extend(result.to_write_tuples())

            # Handle nested Send packets recursively
            if result.send_packets:
                nested_writes = await self._execute_send_packets(
                    list(result.send_packets), config
                )
                if self._pending_interrupt is not None:
                    return all_writes
                all_writes.extend(nested_writes)

        return all_writes

    async def _execute_resumed_node(
        self,
        node_name: str,
        input_state: dict[str, Any],
        config: dict[str, Any],
    ) -> list[tuple[str, Any]]:
        """Execute the interrupted node with the resume value."""
        self._step_counter += 1

        # Prepare store snapshot for the activity
        store_snapshot = self._prepare_store_snapshot()

        # Build activity input with resume value
        activity_input = NodeActivityInput(
            node_name=node_name,
            task_id=f"resume-{node_name}-{self._invocation_counter}",
            graph_id=self.graph_id,
            input_state=input_state,
            config=self._filter_config(config),
            path=tuple(),
            triggers=[],
            resume_value=self._resume_value,
            store_snapshot=store_snapshot,
        )

        # Get node-specific configuration
        activity_options = self._get_node_activity_options(node_name)

        # Generate unique activity ID
        invocation_id = config.get("configurable", {}).get(
            "invocation_id", self._invocation_counter
        )
        activity_id = f"inv{invocation_id}-resume-{node_name}-{self._step_counter}"

        # Execute activity
        result = await workflow.execute_activity(
            resume_langgraph_node,
            activity_input,
            activity_id=activity_id,
            summary=node_name,
            **activity_options,
        )

        # Apply store writes from the activity
        if result.store_writes:
            self._apply_store_writes(result.store_writes)

        # Check if the node interrupted again
        if result.interrupt is not None:
            # Update interrupted state
            self._interrupted_state = input_state
            self._interrupted_node_name = node_name
            self._pending_interrupt = result.interrupt
            return []

        # Mark resume as consumed
        self._resume_used = True

        # Convert ChannelWrite objects to tuples
        return result.to_write_tuples()

    def _filter_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Filter configuration to remove internal LangGraph keys."""
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
        """Get Temporal-specific metadata for a node."""
        node = self.pregel.nodes.get(node_name)
        if node is None:
            return {}
        metadata = getattr(node, "metadata", None) or {}
        return metadata.get("temporal", {})

    def _get_node_activity_options(self, node_name: str) -> dict[str, Any]:
        """Get activity options for a node, merging defaults and metadata."""
        from temporalio.common import Priority, RetryPolicy
        from temporalio.workflow import ActivityCancellationType, VersioningIntent

        node_metadata = self._get_node_metadata(node_name)
        compile_node_options = self.per_node_activity_options.get(node_name, {})
        # Merge: default_activity_options < per_node_activity_options < node metadata from add_node
        temporal_config = {
            **self.default_activity_options,
            **compile_node_options,
            **node_metadata,
        }
        options: dict[str, Any] = {}

        # start_to_close_timeout (required, with default)
        # Check new key first, fall back to legacy key
        timeout = temporal_config.get(
            "start_to_close_timeout", temporal_config.get("activity_timeout")
        )
        if isinstance(timeout, timedelta):
            options["start_to_close_timeout"] = timeout
        else:
            options["start_to_close_timeout"] = timedelta(minutes=5)

        # task_queue (optional)
        task_queue = temporal_config.get("task_queue")
        if isinstance(task_queue, str):
            options["task_queue"] = task_queue

        # heartbeat_timeout (optional)
        heartbeat = temporal_config.get("heartbeat_timeout")
        if isinstance(heartbeat, timedelta):
            options["heartbeat_timeout"] = heartbeat

        # retry_policy priority: node metadata > per_node_activity_options > LangGraph native > default_activity_options > built-in
        node_policy = node_metadata.get("retry_policy") or compile_node_options.get(
            "retry_policy"
        )
        if isinstance(node_policy, RetryPolicy):
            # Node metadata has explicit Temporal RetryPolicy
            options["retry_policy"] = node_policy
        else:
            # Check for LangGraph native retry_policy on node
            node = self.pregel.nodes.get(node_name)
            retry_policies = getattr(node, "retry_policy", None) if node else None
            if retry_policies and len(retry_policies) > 0:
                # LangGraph stores as tuple, use first policy
                lg_policy = retry_policies[0]
                options["retry_policy"] = RetryPolicy(
                    initial_interval=timedelta(seconds=lg_policy.initial_interval),
                    backoff_coefficient=lg_policy.backoff_factor,
                    maximum_interval=timedelta(seconds=lg_policy.max_interval),
                    maximum_attempts=lg_policy.max_attempts,
                )
            elif isinstance(
                self.default_activity_options.get("retry_policy"), RetryPolicy
            ):
                # Use default_activity_options retry_policy
                options["retry_policy"] = self.default_activity_options["retry_policy"]
            else:
                # Built-in default
                options["retry_policy"] = RetryPolicy(maximum_attempts=3)

        # schedule_to_close_timeout (optional)
        schedule_to_close = temporal_config.get("schedule_to_close_timeout")
        if isinstance(schedule_to_close, timedelta):
            options["schedule_to_close_timeout"] = schedule_to_close

        # schedule_to_start_timeout (optional)
        schedule_to_start = temporal_config.get("schedule_to_start_timeout")
        if isinstance(schedule_to_start, timedelta):
            options["schedule_to_start_timeout"] = schedule_to_start

        # cancellation_type (optional)
        cancellation_type = temporal_config.get("cancellation_type")
        if isinstance(cancellation_type, ActivityCancellationType):
            options["cancellation_type"] = cancellation_type

        # versioning_intent (optional)
        versioning_intent = temporal_config.get("versioning_intent")
        if isinstance(versioning_intent, VersioningIntent):
            options["versioning_intent"] = versioning_intent

        # summary (optional)
        summary = temporal_config.get("summary")
        if isinstance(summary, str):
            options["summary"] = summary

        # priority (optional)
        priority = temporal_config.get("priority")
        if isinstance(priority, Priority):
            options["priority"] = priority

        return options

    def invoke(
        self,
        input_state: dict[str, Any],
        config: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Synchronous invoke is not supported. Use ainvoke()."""
        raise NotImplementedError(
            "Synchronous invoke() is not supported in Temporal workflows. "
            "Use ainvoke() instead."
        )

    def get_state(self) -> StateSnapshot:
        """Get the current state snapshot for checkpointing and continue-as-new."""
        # Determine next nodes based on current state
        next_nodes: tuple[str, ...] = ()
        if self._interrupted_node_name is not None:
            next_nodes = (self._interrupted_node_name,)

        # Build tasks tuple with interrupt info if present
        tasks: tuple[dict[str, Any], ...] = ()
        if self._pending_interrupt is not None:
            tasks = (
                {
                    "interrupt_value": self._pending_interrupt.value,
                    "interrupt_node": self._pending_interrupt.node_name,
                    "interrupt_task_id": self._pending_interrupt.task_id,
                },
            )

        # For values, prefer interrupted_state when there's an interrupt
        # (since _last_output only contains the interrupt marker, not the full state)
        # Otherwise use _last_output for completed executions
        if self._interrupted_state is not None:
            values = self._interrupted_state
        else:
            values = self._last_output or {}

        return StateSnapshot(
            values=values,
            next=next_nodes,
            metadata={
                "step": self._step_counter,
                "invocation_counter": self._invocation_counter,
                "completed_nodes": list(self._completed_nodes_in_cycle),
            },
            tasks=tasks,
            store_state=self._serialize_store_state(),
        )

    def _restore_from_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        """Restore runner state from a checkpoint."""
        # Restore state values
        self._last_output = checkpoint.get("values")
        self._interrupted_state = checkpoint.get("values")

        # Restore next node (interrupted node)
        next_nodes = checkpoint.get("next", ())
        if next_nodes:
            self._interrupted_node_name = next_nodes[0]

        # Restore metadata
        metadata = checkpoint.get("metadata", {})
        self._step_counter = metadata.get("step", 0)
        self._invocation_counter = metadata.get("invocation_counter", 0)
        self._completed_nodes_in_cycle = set(metadata.get("completed_nodes", []))

        # Restore interrupt info from tasks
        tasks = checkpoint.get("tasks", ())
        if tasks:
            task = tasks[0]
            self._pending_interrupt = InterruptValue(
                value=task.get("interrupt_value"),
                node_name=task.get("interrupt_node", ""),
                task_id=task.get("interrupt_task_id", ""),
            )

        # Restore store state
        store_state = checkpoint.get("store_state", {})
        self._store_state = {
            (tuple(item["namespace"]), item["key"]): item["value"]
            for item in store_state
        }

    def _prepare_store_snapshot(self) -> Optional[StoreSnapshot]:
        """Prepare a store snapshot for activity input."""
        if not self._store_state:
            return None

        items = [
            StoreItem(namespace=ns, key=key, value=value)
            for (ns, key), value in self._store_state.items()
        ]
        return StoreSnapshot(items=items)

    def _apply_store_writes(self, writes: list[StoreWrite]) -> None:
        """Apply store writes from an activity to the workflow store state."""
        for write in writes:
            key = (tuple(write.namespace), write.key)
            if write.operation == "put" and write.value is not None:
                self._store_state[key] = write.value
            elif write.operation == "delete":
                self._store_state.pop(key, None)

    def _serialize_store_state(self) -> list[dict[str, Any]]:
        """Serialize store state for checkpoint."""
        return [
            {"namespace": list(ns), "key": key, "value": value}
            for (ns, key), value in self._store_state.items()
        ]
