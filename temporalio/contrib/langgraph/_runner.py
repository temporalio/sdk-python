"""Temporal runner for LangGraph graphs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable, cast

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.contrib.langgraph._activities import (
        langgraph_node,
        langgraph_tool_node,
        resume_langgraph_node,
    )

from temporalio.contrib.langgraph._constants import (
    BRANCH_PREFIX,
    CHECKPOINT_KEY,
    INTERRUPT_KEY,
    MODEL_NAME_ATTRS,
    MODEL_NODE_NAMES,
    START_NODE,
    TOOLS_NODE,
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


@dataclass
class InterruptState:
    """State related to interrupt handling in the runner.

    Groups variables that track interrupt status, resume values,
    and pending interrupts during graph execution.
    """

    interrupted_state: dict[str, Any] | None = None
    """State snapshot when interrupt occurred."""

    interrupted_node_name: str | None = None
    """Name of the node that triggered the interrupt."""

    resume_value: Any | None = None
    """Value to resume with after interrupt."""

    resume_used: bool = False
    """Whether the resume value has been consumed."""

    is_resume_invocation: bool = False
    """Whether current invocation is resuming from interrupt."""

    pending_interrupt: InterruptValue | None = None
    """Pending interrupt from current execution."""


@dataclass
class ExecutionState:
    """State related to execution tracking in the runner.

    Groups variables that track execution progress, completed nodes,
    and cached writes during graph execution.
    """

    step_counter: int = 0
    """Counter for unique activity IDs within a step."""

    invocation_counter: int = 0
    """Counter for unique activity IDs across replays."""

    completed_nodes_in_cycle: set[str] = field(default_factory=set)
    """Nodes completed in current resume cycle (to avoid re-execution)."""

    resumed_node_writes: dict[str, list[tuple[str, Any]]] = field(default_factory=dict)
    """Cached writes from resumed nodes (injected to trigger successors)."""

    last_output: dict[str, Any] | None = None
    """Last output state for get_state()."""

    pending_parent_command: Any | None = None
    """Pending parent command from subgraph (for parent graph routing)."""

    store_state: dict[tuple[tuple[str, ...], str], dict[str, Any]] = field(
        default_factory=dict
    )
    """Store state for cross-node persistence."""


def _extract_model_name(node_metadata: dict[str, Any] | None) -> str | None:
    """Extract model name from node metadata if available.

    Looks for model name in metadata that may have been set by the LLM binding.
    """
    if not node_metadata:
        return None

    # Check for model_name in metadata (set by some LLM wrappers)
    model_name = node_metadata.get("model_name")
    if model_name:
        return str(model_name)

    # Check for ls_model_name (LangSmith model name convention)
    ls_model_name = node_metadata.get("ls_model_name")
    if ls_model_name:
        return str(ls_model_name)

    return None


def _extract_last_human_message(input_state: Any, max_length: int = 80) -> str | None:
    """Extract the last human message content from input state.

    For agent workflows, this is typically the user's query.
    """
    if not isinstance(input_state, dict):
        return None

    messages = input_state.get("messages", [])
    if not messages:
        return None

    # Find the last human message (searching from end)
    for msg in reversed(messages):
        msg_type = None
        content = None

        if hasattr(msg, "type"):
            msg_type = msg.type
            content = getattr(msg, "content", None)
        elif isinstance(msg, dict):
            msg_type = msg.get("type")
            content = msg.get("content")

        if msg_type == "human" and content:
            content_str = str(content)
            if len(content_str) > max_length:
                return content_str[: max_length - 3] + "..."
            return content_str

    return None


def _build_activity_summary(
    node_name: str,
    input_state: Any,
    node_metadata: dict[str, Any] | None = None,
    max_length: int = 100,
) -> str:
    """Build a meaningful activity summary from node name, input state, and metadata.

    For tool nodes, extracts tool call information from messages or Send packets.
    For model/agent nodes, shows model name and user query if available.
    For other nodes, uses metadata description if available, otherwise node name.
    """
    # For "tools" node (ToolNode from create_agent/create_react_agent), extract tool calls
    if node_name == TOOLS_NODE and isinstance(input_state, dict):
        tool_calls: list[str] = []

        # Case 1: Send packet with tool_call_with_context (from create_agent/create_react_agent)
        # Structure: {"__type": "tool_call_with_context", "tool_call": {...}, "state": {...}}
        if input_state.get("__type") == "tool_call_with_context":
            tool_call = input_state.get("tool_call", {})
            name = tool_call.get("name", "unknown")
            args = tool_call.get("args", {})
            args_str = str(args)
            tool_calls.append(f"{name}({args_str})")

        # Case 2: Regular state with messages containing tool_calls
        else:
            messages = input_state.get("messages", [])
            for msg in messages:
                # Check for tool_calls attribute (AIMessage with tool calls)
                calls = None
                if hasattr(msg, "tool_calls"):
                    calls = msg.tool_calls
                elif isinstance(msg, dict) and "tool_calls" in msg:
                    calls = msg["tool_calls"]

                if calls:
                    for call in calls:
                        if isinstance(call, dict):
                            name = call.get("name", "unknown")
                            args = call.get("args", {})
                        else:
                            name = getattr(call, "name", "unknown")
                            args = getattr(call, "args", {})

                        args_str = str(args)
                        tool_calls.append(f"{name}({args_str})")

        if tool_calls:
            summary = ", ".join(tool_calls)
            if len(summary) > max_length:
                summary = summary[: max_length - 3] + "..."
            return summary

    # For model/agent nodes, build a summary with model name and query
    if node_name in MODEL_NODE_NAMES and isinstance(input_state, dict):
        parts: list[str] = []

        # Try to get model name from metadata
        model_name = _extract_model_name(node_metadata)
        if model_name:
            parts.append(model_name)
        else:
            parts.append(node_name)

        # Try to extract the user query from messages
        query = _extract_last_human_message(input_state, max_length=60)
        if query:
            parts.append(f'"{query}"')

        if len(parts) > 1:
            summary = ": ".join(parts)
            if len(summary) > max_length:
                summary = summary[: max_length - 3] + "..."
            return summary

    # For other nodes, try to extract query-like fields from input state
    # Common field names for search/query operations
    if isinstance(input_state, dict):
        query_fields = ["query", "search_query", "question", "input", "text", "prompt"]
        for field in query_fields:
            value = input_state.get(field)
            if value and isinstance(value, str):
                truncated = value if len(value) <= 60 else value[:57] + "..."
                summary = f'{node_name}: "{truncated}"'
                if len(summary) > max_length:
                    summary = summary[: max_length - 3] + "..."
                return summary

    # Check for description in node metadata
    if node_metadata and isinstance(node_metadata, dict):
        description = node_metadata.get("description")
        if description and isinstance(description, str):
            if len(description) > max_length:
                return description[: max_length - 3] + "..."
            return description

    return node_name


class TemporalLangGraphRunner:
    """Runner that executes LangGraph graphs with Temporal activities.

    Wraps a compiled Pregel graph and executes nodes as Temporal activities.
    Uses AsyncPregelLoop for graph orchestration. Supports interrupts via
    LangGraph's native API (``INTERRUPT_KEY`` key and ``Command(resume=...)``).
    """

    def __init__(
        self,
        pregel: Pregel,
        graph_id: str,
        default_activity_options: dict[str, Any] | None = None,
        per_node_activity_options: dict[str, dict[str, Any]] | None = None,
        checkpoint: dict[str, Any] | None = None,
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

        # Initialize grouped state
        self._interrupt = InterruptState()
        self._execution = ExecutionState()

        # Restore from checkpoint if provided
        if checkpoint is not None:
            self._restore_from_checkpoint(checkpoint)

    async def ainvoke(
        self,
        input_state: dict[str, Any] | Any,
        config: dict[str, Any] | None = None,
        *,
        should_continue: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Execute the graph asynchronously.

        Args:
            input_state: Initial state or ``Command(resume=value)`` to resume.
            config: Optional configuration for the execution.
            should_continue: Callable returning False to stop for checkpointing.

        Returns:
            Final state. May contain ``INTERRUPT_KEY`` or ``CHECKPOINT_KEY`` keys.
        """
        workflow.logger.debug("Starting graph execution for %s", self.graph_id)

        # Prepare invocation state and detect resume
        input_state, is_resume = self._prepare_invocation_state(input_state)

        # Prepare config with defaults
        config = self._prepare_config(config)

        # Handle resume: execute interrupted node first
        if is_resume and self._interrupt.interrupted_node_name:
            early_return = await self._handle_resume_execution(input_state, config)
            if early_return is not None:
                return early_return

        # Create and run the Pregel loop
        output, interrupted = await self._run_pregel_loop(
            input_state, config, should_continue
        )

        # If we got an early return (checkpoint), return it directly
        if CHECKPOINT_KEY in output:
            return output

        # Finalize output with interrupt markers
        return self._finalize_output(output, interrupted)

    def _prepare_invocation_state(
        self, input_state: dict[str, Any] | Any
    ) -> tuple[dict[str, Any], bool]:
        """Prepare input state and detect if this is a resume invocation.

        Args:
            input_state: Initial state or Command(resume=value).

        Returns:
            Tuple of (prepared_input_state, is_resume).

        Raises:
            ValueError: If resuming without previous interrupt state.
        """
        with workflow.unsafe.imports_passed_through():
            from langgraph.types import Command

        resume_value: Any | None = None
        is_resume = False

        if isinstance(input_state, Command):
            is_resume = True
            if hasattr(input_state, "resume") and input_state.resume is not None:
                resume_value = input_state.resume
            if self._interrupt.interrupted_state is None:
                raise ValueError(
                    "Cannot resume with Command - no previous interrupt state. "
                    "Call ainvoke() first and check for INTERRUPT_KEY in the result."
                )
            input_state = self._interrupt.interrupted_state
        else:
            self._execution.completed_nodes_in_cycle.clear()

        # Update instance state for this invocation
        self._interrupt.resume_value = resume_value
        self._interrupt.resume_used = False
        self._interrupt.is_resume_invocation = is_resume
        self._interrupt.pending_interrupt = None
        self._execution.invocation_counter += 1
        self._execution.step_counter = 0

        return input_state, is_resume

    def _prepare_config(self, config: dict[str, Any] | None) -> dict[str, Any]:
        """Prepare configuration with required defaults.

        Args:
            config: Optional configuration dict.

        Returns:
            Configuration dict with required structure.
        """
        config = config or {}
        if "configurable" not in config:
            config["configurable"] = {}
        if "recursion_limit" not in config:
            config["recursion_limit"] = 25
        return config

    async def _handle_resume_execution(
        self, input_state: dict[str, Any], config: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Handle resume by executing the interrupted node first.

        Executes the interrupted node with the resume value and caches its
        writes for the trigger mechanism.

        Args:
            input_state: Current input state (from interrupted state).
            config: Execution configuration.

        Returns:
            Early return dict if node interrupted again, None otherwise.
        """
        with workflow.unsafe.imports_passed_through():
            from langgraph.types import Interrupt

        interrupted_node = self._interrupt.interrupted_node_name
        assert interrupted_node is not None  # Caller checks this

        resume_writes = await self._execute_resumed_node(
            interrupted_node, input_state, config
        )

        if self._interrupt.pending_interrupt is not None:
            # Node interrupted again - return immediately
            interrupt_obj = Interrupt.from_ns(
                value=self._interrupt.pending_interrupt.value,
                ns="",
            )
            return {**input_state, INTERRUPT_KEY: [interrupt_obj]}

        # Merge writes into input_state for final output
        for channel, value in resume_writes:
            input_state[channel] = value

        # Cache writes for trigger mechanism
        self._execution.resumed_node_writes[interrupted_node] = resume_writes

        # Update completed nodes tracking
        self._execution.completed_nodes_in_cycle.discard(START_NODE)
        self._execution.completed_nodes_in_cycle.add(interrupted_node)
        self._interrupt.interrupted_node_name = None

        return None

    def _create_pregel_loop(
        self, input_state: dict[str, Any], config: dict[str, Any]
    ) -> Any:
        """Create an AsyncPregelLoop for graph execution.

        Args:
            input_state: Input state for the loop.
            config: Execution configuration.

        Returns:
            Configured AsyncPregelLoop instance.
        """
        with workflow.unsafe.imports_passed_through():
            from langgraph.pregel._loop import AsyncPregelLoop

        return AsyncPregelLoop(
            input=input_state,
            stream=None,
            config=cast("RunnableConfig", config),
            store=getattr(self.pregel, "store", None),
            cache=getattr(self.pregel, "cache", None),
            checkpointer=None,
            nodes=self.pregel.nodes,
            specs=self.pregel.channels,
            trigger_to_nodes=getattr(self.pregel, "trigger_to_nodes", {}),
            durability="sync",
            input_keys=getattr(self.pregel, "input_channels", None) or [],
            output_keys=getattr(self.pregel, "output_channels", None) or [],
            stream_keys=getattr(self.pregel, "stream_channels_asis", None) or [],
        )

    async def _run_pregel_loop(
        self,
        input_state: dict[str, Any],
        config: dict[str, Any],
        should_continue: Callable[[], bool] | None,
    ) -> tuple[dict[str, Any], bool]:
        """Run the Pregel loop to execute the graph.

        Args:
            input_state: Input state for the loop.
            config: Execution configuration.
            should_continue: Optional callable to check for checkpointing.

        Returns:
            Tuple of (output_dict, was_interrupted).
        """
        loop = self._create_pregel_loop(input_state, config)

        await loop.__aenter__()
        interrupted = False

        try:
            while loop.tick():
                # Inject cached writes for resumed nodes
                self._inject_resumed_writes(loop)

                # Get executable tasks
                tasks_to_execute = self._get_executable_tasks(loop)

                # No tasks - process writes and check for checkpoint
                if not tasks_to_execute:
                    loop.after_tick()
                    checkpoint_output = self._check_checkpoint(loop, should_continue)
                    if checkpoint_output is not None:
                        return checkpoint_output, False
                    continue

                # Execute tasks
                task_interrupted = await self._execute_loop_tasks(
                    tasks_to_execute, loop
                )

                if task_interrupted:
                    loop.after_tick()
                    interrupted = True
                    break

                loop.after_tick()

                # Check for checkpoint after successful tick
                checkpoint_output = self._check_checkpoint(loop, should_continue)
                if checkpoint_output is not None:
                    return checkpoint_output, False

        finally:
            if not interrupted:
                await loop.__aexit__(None, None, None)

        output = cast("dict[str, Any]", loop.output) if loop.output else {}
        return output, interrupted

    def _inject_resumed_writes(self, loop: Any) -> None:
        """Inject cached writes from resumed nodes into loop tasks.

        This allows the trigger mechanism to schedule successor nodes.
        """
        for task in loop.tasks.values():
            if task.name in self._execution.resumed_node_writes:
                cached_writes = self._execution.resumed_node_writes.pop(task.name)
                task.writes.extend(cached_writes)

    def _get_executable_tasks(self, loop: Any) -> list[Any]:
        """Get tasks that need to be executed.

        Filters out tasks that already have writes or were completed
        in the current resume cycle.
        """
        return [
            task
            for task in loop.tasks.values()
            if not task.writes
            and task.name not in self._execution.completed_nodes_in_cycle
        ]

    def _check_checkpoint(
        self, loop: Any, should_continue: Callable[[], bool] | None
    ) -> dict[str, Any] | None:
        """Check if we should stop for checkpointing.

        Args:
            loop: The Pregel loop.
            should_continue: Optional callable to check for checkpointing.

        Returns:
            Output dict with checkpoint if stopping, None otherwise.
        """
        if should_continue is not None and not should_continue():
            output = cast("dict[str, Any]", loop.output) if loop.output else {}
            output[CHECKPOINT_KEY] = self.get_state()
            self._execution.last_output = output
            return output
        return None

    async def _execute_loop_tasks(self, tasks: list[Any], loop: Any) -> bool:
        """Execute a list of tasks in parallel (BSP model).

        LangGraph uses a Bulk Synchronous Parallel (BSP) model where all tasks
        that are ready to execute in a step run concurrently. This method
        executes all tasks in parallel using asyncio.gather().

        Args:
            tasks: List of tasks to execute.
            loop: The Pregel loop.

        Returns:
            True if any task was interrupted, False otherwise.
        """
        if not tasks:
            return False

        # Execute all tasks in parallel
        results = await asyncio.gather(
            *[self._execute_task(task, loop) for task in tasks]
        )

        # Check if any task was interrupted (returned False)
        # In BSP model, if any task interrupts, the step is interrupted
        return not all(results)

    def _finalize_output(
        self, output: dict[str, Any], interrupted: bool
    ) -> dict[str, Any]:
        """Finalize the output with interrupt markers and logging.

        Args:
            output: Raw output from the loop.
            interrupted: Whether execution was interrupted.

        Returns:
            Final output dict.
        """
        with workflow.unsafe.imports_passed_through():
            from langgraph.types import Interrupt

        if self._interrupt.pending_interrupt is not None:
            interrupt_obj = Interrupt.from_ns(
                value=self._interrupt.pending_interrupt.value,
                ns="",
            )
            output = {**output, INTERRUPT_KEY: [interrupt_obj]}

        self._execution.last_output = output

        if INTERRUPT_KEY in output:
            workflow.logger.debug(
                "Graph %s execution paused at interrupt", self.graph_id
            )
        elif CHECKPOINT_KEY in output:
            workflow.logger.debug(
                "Graph %s execution stopped for checkpoint", self.graph_id
            )
        else:
            workflow.logger.debug("Graph %s execution completed", self.graph_id)

        return output

    async def _execute_task(self, task: PregelExecutableTask, loop: Any) -> bool:
        """Execute a single task. Returns False if interrupted."""
        # Determine if this task should receive the resume value
        # Only pass resume value to the specific node that was interrupted
        resume_for_task = None
        if (
            self._interrupt.resume_value is not None
            and not self._interrupt.resume_used
            and self._interrupt.interrupted_node_name == task.name
        ):
            # This is the node that was interrupted - pass the resume value
            resume_for_task = self._interrupt.resume_value

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
        if self._interrupt.pending_interrupt is not None:
            # The task interrupted - don't mark resume as used
            return False

        # Task completed successfully - track it to prevent re-execution during resume
        # Only track during resume invocations to allow normal cyclic execution
        if self._interrupt.is_resume_invocation:
            self._execution.completed_nodes_in_cycle.add(task.name)

        # If we provided a resume value and the task completed successfully,
        # it means the task consumed the resume value (interrupt() returned it)
        if resume_for_task is not None:
            self._interrupt.resume_used = True

        # Record writes to the loop
        # This is how activity results flow back into the Pregel state
        task.writes.extend(writes)

        # Handle Send packets - execute each as a separate task
        # Send creates dynamic tasks with custom input (Send.arg)
        if send_packets:
            send_writes = await self._execute_send_packets(send_packets, task.config)
            if self._interrupt.pending_interrupt is not None:
                return False
            task.writes.extend(send_writes)

        return True

    def _should_run_in_workflow(self, node_name: str) -> bool:
        """Check if a node should run directly in the workflow."""
        # START_NODE is a built-in LangGraph node that only forwards input to
        # state channels. It performs no I/O or non-deterministic operations,
        # so it can safely run inline in the workflow.
        if node_name == START_NODE:
            return True

        # Check node metadata
        node = self.pregel.nodes.get(node_name)
        if node is None:
            return False

        # Look for temporal.run_in_workflow in metadata
        metadata = getattr(node, "metadata", None) or {}
        temporal_config = metadata.get("temporal", {})
        return temporal_config.get("run_in_workflow", False)

    def _get_subgraph(self, node_name: str) -> "Pregel | None":
        """Get the subgraph for a node if it exists.

        A node is a subgraph if it has a compiled LangGraph (Pregel) as its
        bound runnable. This is detected via the node's subgraphs attribute
        which is populated by LangGraph during graph construction.

        Args:
            node_name: Name of the node to check.

        Returns:
            The subgraph's Pregel instance if the node is a subgraph, None otherwise.
        """
        node = self.pregel.nodes.get(node_name)
        if node is None:
            return None

        # Check if node has subgraphs (populated by LangGraph's find_subgraph_pregel)
        subgraphs = getattr(node, "subgraphs", None)
        if subgraphs and len(subgraphs) > 0:
            # Return the first (and typically only) subgraph
            return subgraphs[0]

        return None

    def _create_nested_runner(
        self,
        task: "PregelExecutableTask",
        subgraph: "Pregel",
    ) -> "TemporalLangGraphRunner":
        """Create a nested runner for executing a subgraph.

        Args:
            task: The task representing the subgraph node.
            subgraph: The subgraph's Pregel instance.

        Returns:
            A new TemporalLangGraphRunner configured for the subgraph.
        """
        subgraph_id = f"{self.graph_id}:{task.name}"
        return self.__class__(
            pregel=subgraph,
            graph_id=subgraph_id,
            default_activity_options={"temporal": self.default_activity_options},
            per_node_activity_options={
                k.split(":", 1)[1]: v
                for k, v in self.per_node_activity_options.items()
                if k.startswith(f"{task.name}:")
            },
        )

    def _handle_subgraph_interrupt(
        self,
        task: "PregelExecutableTask",
        result: dict[str, Any],
    ) -> bool:
        """Handle interrupt propagation from a subgraph.

        Args:
            task: The task representing the subgraph node.
            result: The result from the subgraph execution.

        Returns:
            True if an interrupt was handled, False otherwise.
        """
        if INTERRUPT_KEY not in result:
            return False

        self._interrupt.interrupted_state = cast("dict[str, Any]", task.input)
        self._interrupt.interrupted_node_name = task.name

        with workflow.unsafe.imports_passed_through():
            from langgraph.types import Interrupt

        interrupt_list = result.get(INTERRUPT_KEY, [])
        if interrupt_list:
            interrupt_obj = interrupt_list[0]
            interrupt_value = (
                interrupt_obj.value
                if isinstance(interrupt_obj, Interrupt)
                else interrupt_obj
            )
            self._interrupt.pending_interrupt = InterruptValue(
                value=interrupt_value,
                node_name=task.name,
                task_id=task.id,
            )
        return True

    def _handle_subgraph_parent_command(
        self,
        nested_runner: "TemporalLangGraphRunner",
        task: "PregelExecutableTask",
        result: dict[str, Any],
    ) -> list[Any]:
        """Handle parent command from a subgraph.

        Args:
            nested_runner: The nested runner that executed the subgraph.
            task: The task representing the subgraph node.
            result: The result from the subgraph execution.

        Returns:
            List of SendPackets for routing in the parent graph.
        """
        if nested_runner._execution.pending_parent_command is None:
            return []

        cmd = nested_runner._execution.pending_parent_command
        workflow.logger.debug(
            "Subgraph %s has pending parent command: goto=%s",
            task.name,
            cmd.goto,
        )

        if not cmd.goto:
            return []

        from temporalio.contrib.langgraph._models import SendPacket

        return [SendPacket(node=node_name, arg=result) for node_name in cmd.goto]

    def _extract_subgraph_writes(
        self,
        result: dict[str, Any],
    ) -> list[tuple[str, Any]]:
        """Extract writes from subgraph result.

        Args:
            result: The result from the subgraph execution.

        Returns:
            List of (channel, value) tuples for non-internal keys.
        """
        return [
            (key, value) for key, value in result.items() if not key.startswith("__")
        ]

    def _invoke_subgraph_writers(
        self,
        task: "PregelExecutableTask",
        result: dict[str, Any],
    ) -> list[tuple[str, Any]]:
        """Invoke parent node writers to get proper edge routing.

        Writers handle both static edges and conditional edges (routing functions).
        By invoking writers with the merged state, we get the correct branch writes.

        Args:
            task: The task representing the subgraph node.
            result: The result from the subgraph execution.

        Returns:
            List of branch writes (channel, value) tuples.
        """
        parent_node = self.pregel.nodes.get(task.name)
        if parent_node is None:
            return []

        node_writers = getattr(parent_node, "writers", None)
        if not node_writers:
            return []

        branch_writes: list[tuple[str, Any]] = []

        with workflow.unsafe.imports_passed_through():
            from collections import deque

            from langgraph.constants import CONFIG_KEY_READ, CONFIG_KEY_SEND

            merged_state = {**cast("dict[str, Any]", task.input), **result}
            writer_writes: deque[tuple[str, Any]] = deque()

            def read_state(channel: Any, fresh: bool = False) -> Any:
                if isinstance(channel, str):
                    return merged_state.get(channel)
                return {c: merged_state.get(c) for c in channel}

            writer_config = {
                **cast("dict[str, Any]", task.config),
                "configurable": {
                    **cast("dict[str, Any]", task.config).get("configurable", {}),
                    CONFIG_KEY_SEND: writer_writes.extend,
                    CONFIG_KEY_READ: read_state,
                },
            }

            for writer in node_writers:
                try:
                    if hasattr(writer, "invoke"):
                        writer.invoke(merged_state, writer_config)
                except Exception as e:
                    workflow.logger.warning(
                        "Writer invocation failed for node %s: %s: %s",
                        task.name,
                        type(e).__name__,
                        e,
                    )

            for channel, value in writer_writes:
                if channel.startswith(BRANCH_PREFIX):
                    branch_writes.append((channel, value))
                    workflow.logger.debug(
                        "Subgraph %s produced branch write: %s",
                        task.name,
                        channel,
                    )

        return branch_writes

    async def _execute_subgraph(
        self,
        task: "PregelExecutableTask",
        subgraph: "Pregel",
        resume_value: Any | None = None,
    ) -> tuple[list[tuple[str, Any]], list[Any]]:
        """Execute a subgraph node by running its inner nodes as separate activities.

        Instead of running the entire subgraph as a single activity, this method
        creates a nested TemporalRunner for the subgraph and executes it. This
        ensures each inner node (e.g., 'model' and 'tools' in create_agent)
        runs as a separate Temporal activity with its own retry/timeout settings.

        Args:
            task: The task representing the subgraph node.
            subgraph: The subgraph's Pregel instance.
            resume_value: Optional resume value for interrupt handling.

        Returns:
            Tuple of (writes, send_packets) from the subgraph execution.
        """
        workflow.logger.debug(
            "Executing subgraph node %s with %d inner nodes",
            task.name,
            len(subgraph.nodes),
        )

        nested_runner = self._create_nested_runner(task, subgraph)
        config = cast("dict[str, Any]", task.config)
        result = await nested_runner.ainvoke(task.input, config)

        # Handle interrupt propagation
        if self._handle_subgraph_interrupt(task, result):
            return [], []

        # Handle parent command routing
        send_packets = self._handle_subgraph_parent_command(nested_runner, task, result)

        # Extract writes from result
        writes = self._extract_subgraph_writes(result)

        # Invoke writers for edge routing
        branch_writes = self._invoke_subgraph_writers(task, result)
        writes.extend(branch_writes)

        workflow.logger.debug(
            "Subgraph %s returning %d writes, %d send_packets: %s",
            task.name,
            len(writes),
            len(send_packets),
            [w[0] for w in writes],
        )
        return writes, send_packets

    async def _execute_in_workflow(
        self,
        task: PregelExecutableTask,
    ) -> list[tuple[str, Any]]:
        """Execute a task directly in the workflow.

        This is used for nodes marked with run_in_workflow=True, which need
        to call Temporal operations (activities, child workflows, etc.) directly.

        The user's function runs under sandbox restrictions to catch non-deterministic
        code. Only LangGraph setup/cleanup uses sandbox_unrestricted(), following
        the pattern from langchain_interceptor.py.
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

        result: Any = None

        if task.proc is not None:
            runnable_config = cast("RunnableConfig", config)

            # Try to extract the raw user function from the runnable chain.
            # LangGraph wraps user functions in RunnableSeq with steps:
            # - steps[0]: RunnableCallable containing user's func/afunc
            # - steps[1]: ChannelWrite for state management and edge traversal
            raw_func = None
            channel_write_step = None
            steps = getattr(task.proc, "steps", None)
            if steps and len(steps) >= 2:
                first_step = steps[0]
                # RunnableCallable uses afunc for async, func for sync
                raw_func = getattr(first_step, "afunc", None) or getattr(
                    first_step, "func", None
                )
                channel_write_step = steps[1]

            if raw_func is not None and channel_write_step is not None:
                # Execute the user's function SANDBOXED (outside sandbox_unrestricted).
                # This catches non-deterministic code while still allowing
                # Temporal operations (activities, child workflows, etc.).
                if asyncio.iscoroutinefunction(raw_func):
                    result = await raw_func(task.input)
                else:
                    result = raw_func(task.input)

                # Run ChannelWrite step UNRESTRICTED to handle state updates
                # and edge traversal. This follows the langchain_interceptor pattern.
                with workflow.unsafe.sandbox_unrestricted():
                    with workflow.unsafe.imports_passed_through():
                        if asyncio.iscoroutinefunction(
                            getattr(channel_write_step, "ainvoke", None)
                        ):
                            await channel_write_step.ainvoke(result, runnable_config)
                        else:
                            channel_write_step.invoke(result, runnable_config)
            else:
                # Fallback for complex runnables that don't expose steps.
                # Use sandbox_unrestricted() to allow LangGraph machinery to work.
                with workflow.unsafe.sandbox_unrestricted():
                    with workflow.unsafe.imports_passed_through():
                        if asyncio.iscoroutinefunction(
                            getattr(task.proc, "ainvoke", None)
                        ):
                            result = await task.proc.ainvoke(
                                task.input, runnable_config
                            )
                        else:
                            result = task.proc.invoke(task.input, runnable_config)

            # Capture writes from the result if it's a dict
            if isinstance(result, dict):
                for key, value in result.items():
                    if (key, value) not in writes:
                        writes.append((key, value))

        return list(writes)

    async def _execute_as_activity_with_sends(
        self,
        task: PregelExecutableTask,
        resume_value: Any | None = None,
    ) -> tuple[list[tuple[str, Any]], list[Any]]:
        """Execute a task as a Temporal activity, returning writes and send packets."""
        self._execution.step_counter += 1

        # Check if this node is a subgraph - if so, execute it recursively
        # This ensures inner nodes (e.g., 'model' and 'tools' in create_agent)
        # run as separate activities instead of the subgraph running as one activity
        subgraph = self._get_subgraph(task.name)
        if subgraph is not None:
            return await self._execute_subgraph(task, subgraph, resume_value)

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
            "invocation_id", self._execution.invocation_counter
        )
        activity_id = f"inv{invocation_id}-{task.name}-{self._execution.step_counter}"

        # Build meaningful summary from node name, input, and metadata
        node_metadata = self._get_full_node_metadata(task.name)
        summary = _build_activity_summary(task.name, task.input, node_metadata)

        # Use langgraph_tool_node for "tools" node, langgraph_node for others
        activity_fn = langgraph_tool_node if task.name == TOOLS_NODE else langgraph_node

        # Execute activity
        result = await workflow.execute_activity(
            activity_fn,
            activity_input,
            activity_id=activity_id,
            summary=summary,
            **activity_options,
        )

        # Apply store writes from the activity (before checking interrupt)
        if result.store_writes:
            self._apply_store_writes(result.store_writes)

        # Check if the node raised an interrupt
        if result.interrupt is not None:
            self._interrupt.interrupted_state = cast("dict[str, Any]", task.input)
            self._interrupt.interrupted_node_name = task.name
            self._interrupt.pending_interrupt = result.interrupt
            return [], []

        # Check if the node issued a parent command (from subgraph to parent)
        # This happens in supervisor patterns where agent's tool node raises ParentCommand
        # Store it for the parent to handle - don't execute goto in current context
        if result.parent_command is not None:
            cmd = result.parent_command
            writes: list[tuple[str, Any]] = []

            # Convert command.update to writes (state updates for current graph)
            if cmd.update:
                for channel, value in cmd.update.items():
                    writes.append((channel, value))

            # Store the parent command for the parent graph to handle
            # The goto nodes exist in the parent, not in this graph
            self._execution.pending_parent_command = cmd

            return writes, []

        # Return writes and send_packets separately
        return result.to_write_tuples(), list(result.send_packets)

    async def _execute_send_packets(
        self,
        send_packets: list[Any],
        config: Any,
    ) -> list[tuple[str, Any]]:
        """Execute Send packets as separate activities in parallel."""
        all_writes: list[tuple[str, Any]] = []

        if not send_packets:
            return all_writes

        # Phase 1: Prepare all activity inputs
        # We do this first so step counters are assigned consistently
        prepared_activities: list[
            tuple[Any, NodeActivityInput, dict[str, Any], str, str, Callable[..., Any]]
        ] = []

        config_dict = cast("dict[str, Any]", config)
        invocation_id = config_dict.get("configurable", {}).get(
            "invocation_id", self._execution.invocation_counter
        )

        # Prepare store snapshot once - all parallel activities see same snapshot
        store_snapshot = self._prepare_store_snapshot()

        for packet in send_packets:
            self._execution.step_counter += 1

            # Build activity input with Send.arg as the input state
            activity_input = NodeActivityInput(
                node_name=packet.node,
                task_id=f"send-{packet.node}-{self._execution.step_counter}",
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
            activity_id = (
                f"inv{invocation_id}-send-{packet.node}-{self._execution.step_counter}"
            )

            # Build meaningful summary from node name, input, and metadata
            node_metadata = self._get_full_node_metadata(packet.node)
            summary = _build_activity_summary(packet.node, packet.arg, node_metadata)

            # Use langgraph_tool_node for "tools" node, langgraph_node for others
            activity_fn = (
                langgraph_tool_node if packet.node == TOOLS_NODE else langgraph_node
            )

            prepared_activities.append(
                (
                    packet,
                    activity_input,
                    activity_options,
                    activity_id,
                    summary,
                    activity_fn,
                )
            )

        # Phase 2: Execute all activities in parallel
        async def execute_single_activity(
            activity_fn: Callable[..., Any],
            activity_input: NodeActivityInput,
            activity_id: str,
            summary: str,
            activity_options: dict[str, Any],
        ) -> Any:
            return await workflow.execute_activity(
                activity_fn,
                activity_input,
                activity_id=activity_id,
                summary=summary,
                **activity_options,
            )

        tasks = [
            execute_single_activity(
                activity_fn, activity_input, activity_id, summary, activity_options
            )
            for (
                _packet,
                activity_input,
                activity_options,
                activity_id,
                summary,
                activity_fn,
            ) in prepared_activities
        ]

        results = await asyncio.gather(*tasks)

        # Phase 3: Process results sequentially
        # This handles store writes, interrupts, parent commands, and nested sends
        for (packet, _input, _opts, _id, _summary, _fn), result in zip(
            prepared_activities, results
        ):
            # Apply store writes
            if result.store_writes:
                self._apply_store_writes(result.store_writes)

            # Check for interrupt
            if result.interrupt is not None:
                self._interrupt.interrupted_state = packet.arg
                self._interrupt.interrupted_node_name = packet.node
                self._interrupt.pending_interrupt = result.interrupt
                return all_writes

            # Check for parent command (from subgraph to parent)
            # Store it for the parent to handle - don't execute goto in subgraph context
            if result.parent_command is not None:
                cmd = result.parent_command
                # Add writes from command.update (state updates for the subgraph)
                if cmd.update:
                    for channel, value in cmd.update.items():
                        all_writes.append((channel, value))
                # Store the parent command for the parent graph to handle
                # The goto nodes exist in the parent, not in this subgraph
                self._execution.pending_parent_command = cmd
                continue  # Skip normal write/send_packet processing

            # Collect writes
            all_writes.extend(result.to_write_tuples())

            # Handle nested Send packets recursively
            if result.send_packets:
                nested_writes = await self._execute_send_packets(
                    list(result.send_packets), config
                )
                if self._interrupt.pending_interrupt is not None:
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
        self._execution.step_counter += 1

        # Prepare store snapshot for the activity
        store_snapshot = self._prepare_store_snapshot()

        # Build activity input with resume value
        activity_input = NodeActivityInput(
            node_name=node_name,
            task_id=f"resume-{node_name}-{self._execution.invocation_counter}",
            graph_id=self.graph_id,
            input_state=input_state,
            config=self._filter_config(config),
            path=tuple(),
            triggers=[],
            resume_value=self._interrupt.resume_value,
            store_snapshot=store_snapshot,
        )

        # Get node-specific configuration
        activity_options = self._get_node_activity_options(node_name)

        # Generate unique activity ID
        invocation_id = config.get("configurable", {}).get(
            "invocation_id", self._execution.invocation_counter
        )
        activity_id = (
            f"inv{invocation_id}-resume-{node_name}-{self._execution.step_counter}"
        )

        # Build meaningful summary from node name, input, and metadata
        node_metadata = self._get_full_node_metadata(node_name)
        summary = _build_activity_summary(node_name, input_state, node_metadata)

        # Execute activity
        result = await workflow.execute_activity(
            resume_langgraph_node,
            activity_input,
            activity_id=activity_id,
            summary=summary,
            **activity_options,
        )

        # Apply store writes from the activity
        if result.store_writes:
            self._apply_store_writes(result.store_writes)

        # Check if the node interrupted again
        if result.interrupt is not None:
            # Update interrupted state
            self._interrupt.interrupted_state = input_state
            self._interrupt.interrupted_node_name = node_name
            self._interrupt.pending_interrupt = result.interrupt
            return []

        # Mark resume as consumed
        self._interrupt.resume_used = True

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

    def _get_full_node_metadata(self, node_name: str) -> dict[str, Any]:
        """Get full metadata for a node (for activity summaries).

        Also attempts to extract model_name from the node's runnable if it's
        a LangChain chat model (ChatOpenAI, ChatAnthropic, etc.).
        """
        node = self.pregel.nodes.get(node_name)
        if node is None:
            return {}

        metadata = dict(getattr(node, "metadata", None) or {})

        # Try to extract model name from the node's runnable (for chat models)
        # This handles create_react_agent where the model is bound to the node
        if "model_name" not in metadata:
            model_name = self._extract_model_name_from_runnable(node)
            if model_name:
                metadata["model_name"] = model_name

        return metadata

    def _extract_model_name_from_runnable(self, node: Any) -> str | None:
        """Extract model name from a node's runnable if it's a chat model.

        Supports ChatOpenAI, ChatAnthropic, and other LangChain chat models
        that have model_name or model attributes. Also handles create_agent
        where the model is captured in a closure.
        """
        runnable = getattr(node, "node", None)
        if runnable is None:
            return None

        # Try common model name attributes used by LangChain chat models
        # ChatOpenAI uses model_name, ChatAnthropic uses model
        for attr in MODEL_NAME_ATTRS:
            value = getattr(runnable, attr, None)
            if value and isinstance(value, str):
                return value

        # For RunnableSequence or wrapped models, try to find the model in the chain
        # This handles cases like model.bind_tools(...)
        bound = getattr(runnable, "bound", None)
        if bound is not None:
            for attr in MODEL_NAME_ATTRS:
                value = getattr(bound, attr, None)
                if value and isinstance(value, str):
                    return value

        # Try first element if it's a sequence
        first = getattr(runnable, "first", None)
        if first is not None:
            for attr in MODEL_NAME_ATTRS:
                value = getattr(first, attr, None)
                if value and isinstance(value, str):
                    return value

        # For create_agent (LangChain 1.0+), the model is in the closure of the
        # model_node function. The runnable is a RunnableSeq with steps, and
        # the first step is a RunnableCallable wrapping model_node.
        steps = getattr(runnable, "steps", None)
        if steps and len(steps) > 0:
            first_step = steps[0]
            func = getattr(first_step, "func", None)
            if func is not None:
                closure = getattr(func, "__closure__", None)
                if closure:
                    for cell in closure:
                        try:
                            obj = cell.cell_contents
                            # Check if this closure variable is a chat model
                            for attr in MODEL_NAME_ATTRS:
                                value = getattr(obj, attr, None)
                                if value and isinstance(value, str):
                                    return value
                        except ValueError:
                            # Empty cell
                            continue

        return None

    def _get_node_metadata(self, node_name: str) -> dict[str, Any]:
        """Get Temporal-specific metadata for a node."""
        return self._get_full_node_metadata(node_name).get("temporal", {})

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
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Raise NotImplementedError since sync execution is unsupported."""
        raise NotImplementedError(
            "Synchronous invoke() is not supported in Temporal workflows. "
            "Use ainvoke() instead."
        )

    def get_state(self) -> StateSnapshot:
        """Get the current state snapshot for checkpointing and continue-as-new."""
        # Determine next nodes based on current state
        next_nodes: tuple[str, ...] = ()
        if self._interrupt.interrupted_node_name is not None:
            next_nodes = (self._interrupt.interrupted_node_name,)

        # Build tasks tuple with interrupt info if present
        tasks: tuple[dict[str, Any], ...] = ()
        if self._interrupt.pending_interrupt is not None:
            tasks = (
                {
                    "interrupt_value": self._interrupt.pending_interrupt.value,
                    "interrupt_node": self._interrupt.pending_interrupt.node_name,
                    "interrupt_task_id": self._interrupt.pending_interrupt.task_id,
                },
            )

        # For values, prefer interrupted_state when there's an interrupt
        # (since _last_output only contains the interrupt marker, not the full state)
        # Otherwise use _last_output for completed executions
        if self._interrupt.interrupted_state is not None:
            values = self._interrupt.interrupted_state
        else:
            values = self._execution.last_output or {}

        return StateSnapshot(
            values=values,
            next=next_nodes,
            metadata={
                "step": self._execution.step_counter,
                "invocation_counter": self._execution.invocation_counter,
                "completed_nodes": list(self._execution.completed_nodes_in_cycle),
            },
            tasks=tasks,
            store_state=self._serialize_store_state(),
        )

    def _restore_from_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        """Restore runner state from a checkpoint."""
        # Restore state values
        self._execution.last_output = checkpoint.get("values")
        self._interrupt.interrupted_state = checkpoint.get("values")

        # Restore next node (interrupted node)
        next_nodes = checkpoint.get("next", ())
        if next_nodes:
            self._interrupt.interrupted_node_name = next_nodes[0]

        # Restore metadata
        metadata = checkpoint.get("metadata", {})
        self._execution.step_counter = metadata.get("step", 0)
        self._execution.invocation_counter = metadata.get("invocation_counter", 0)
        self._execution.completed_nodes_in_cycle = set(
            metadata.get("completed_nodes", [])
        )

        # Restore interrupt info from tasks
        tasks = checkpoint.get("tasks", ())
        if tasks:
            task = tasks[0]
            self._interrupt.pending_interrupt = InterruptValue(
                value=task.get("interrupt_value"),
                node_name=task.get("interrupt_node", ""),
                task_id=task.get("interrupt_task_id", ""),
            )

        # Restore store state
        store_state = checkpoint.get("store_state", {})
        self._execution.store_state = {
            (tuple(item["namespace"]), item["key"]): item["value"]
            for item in store_state
        }

    def _prepare_store_snapshot(self) -> StoreSnapshot | None:
        """Prepare a store snapshot for activity input."""
        if not self._execution.store_state:
            return None

        items = [
            StoreItem(namespace=ns, key=key, value=value)
            for (ns, key), value in self._execution.store_state.items()
        ]
        return StoreSnapshot(items=items)

    def _apply_store_writes(self, writes: list[StoreWrite]) -> None:
        """Apply store writes from an activity to the workflow store state."""
        for write in writes:
            key = (tuple(write.namespace), write.key)
            if write.operation == "put" and write.value is not None:
                self._execution.store_state[key] = write.value
            elif write.operation == "delete":
                self._execution.store_state.pop(key, None)

    def _serialize_store_state(self) -> list[dict[str, Any]]:
        """Serialize store state for checkpoint."""
        return [
            {"namespace": list(ns), "key": key, "value": value}
            for (ns, key), value in self._execution.store_state.items()
        ]

    def get_graph_mermaid(self, *, with_styles: bool = True) -> str:
        """Get a Mermaid diagram of the graph with execution state.

        Returns a Mermaid flowchart showing the graph structure with nodes
        colored based on their execution status:
        - Green (completed): Nodes that have finished executing
        - Yellow (current): Node currently executing or interrupted
        - Gray (pending): Nodes not yet executed

        Args:
            with_styles: If True, include CSS class definitions for styling.
                Set to False for simpler output.

        Returns:
            Mermaid diagram string that can be rendered in GitHub, Notion,
            or any Mermaid-compatible viewer.

        Example output::

            graph TD;
                __start__([__start__]):::completed
                validate(validate):::completed
                process(process):::current
                __end__([__end__]):::pending
                __start__ --> validate;
                validate --> process;
                process --> __end__;
                classDef completed fill:#90EE90
                classDef current fill:#FFD700
                classDef pending fill:#D3D3D3
        """
        # Get the base mermaid diagram from LangGraph
        graph = self.pregel.get_graph()
        base_mermaid = graph.draw_mermaid()

        # Determine node statuses
        completed_nodes = set(self._execution.completed_nodes_in_cycle)
        current_node = self._interrupt.interrupted_node_name

        # Build status map
        node_status: dict[str, str] = {}
        for node_name in graph.nodes:
            if node_name == "__start__":
                node_status[node_name] = "first"
            elif node_name == "__end__":
                node_status[node_name] = "last" if not completed_nodes else "pending"
            elif node_name == current_node:
                node_status[node_name] = "current"
            elif node_name in completed_nodes:
                node_status[node_name] = "completed"
            else:
                node_status[node_name] = "pending"

        # Parse and rebuild mermaid with status classes
        lines = base_mermaid.strip().split("\n")
        new_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            # Skip existing classDef lines - we'll add our own
            if stripped.startswith("classDef"):
                continue
            # Add status class to node definitions
            elif (
                stripped
                and not stripped.startswith("---")
                and not stripped.startswith("graph")
                and not stripped.startswith("config:")
                and not stripped.startswith("flowchart:")
                and not stripped.startswith("curve:")
            ):
                # Check if this is a node definition (not an edge)
                if "-->" not in stripped and "-.->" not in stripped:
                    # Find which node this line defines
                    for node_name, status in node_status.items():
                        # Match node definitions like "validate(validate)" or "__start__([...])"
                        if stripped.startswith(f"{node_name}(") or stripped.startswith(
                            f"{node_name}["
                        ):
                            # Remove any existing class and add status class
                            if ":::" in stripped:
                                stripped = stripped.rsplit(":::", 1)[0]
                            line = f"\t{stripped}:::{status}"
                            break
            new_lines.append(line)

        # Add status class definitions if requested
        if with_styles:
            new_lines.append("\tclassDef completed fill:#90EE90,stroke:#228B22")
            new_lines.append("\tclassDef current fill:#FFD700,stroke:#FFA500")
            new_lines.append("\tclassDef pending fill:#D3D3D3,stroke:#A9A9A9")
            new_lines.append("\tclassDef first fill-opacity:0")
            new_lines.append("\tclassDef last fill:#bfb6fc")

        return "\n".join(new_lines)

    def get_graph_ascii(self, *, show_legend: bool = True) -> str:
        """Get an ASCII art diagram of the graph with execution progress.

        Returns a text-based representation of the graph showing:
        - Node execution status with symbols (✓ completed, ▶ current, ○ pending)
        - The graph structure as a vertical flowchart
        - Optional legend explaining the symbols

        Args:
            show_legend: If True, include a legend at the bottom explaining
                the status symbols.

        Returns:
            ASCII art string showing the graph structure and execution state.

        Example output::

            ┌───────────┐
            │   START   │ ✓
            └─────┬─────┘
                  │
                  ▼
            ┌───────────┐
            │  validate │ ✓
            └─────┬─────┘
                  │
                  ▼
            ┌───────────┐
            │  process  │ ▶ INTERRUPTED
            └─────┬─────┘
                  │
                  ▼
            ┌───────────┐
            │    END    │ ○
            └───────────┘

            Legend: ✓ completed  ▶ current/interrupted  ○ pending
        """
        graph = self.pregel.get_graph()

        # Determine node statuses
        completed_nodes = set(self._execution.completed_nodes_in_cycle)
        current_node = self._interrupt.interrupted_node_name
        is_interrupted = self._interrupt.pending_interrupt is not None

        # Get nodes in topological order (simple linear for now)
        # Filter out internal nodes
        visible_nodes = [
            name
            for name in graph.nodes
            if not name.startswith("__") or name in ("__start__", "__end__")
        ]

        # Build edge map for ordering
        edge_map: dict[str, list[str]] = {}
        for edge in graph.edges:
            if edge.source not in edge_map:
                edge_map[edge.source] = []
            edge_map[edge.source].append(edge.target)

        # Simple topological sort for linear graphs
        ordered_nodes: list[str] = []
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visited or node not in visible_nodes:
                return
            visited.add(node)
            ordered_nodes.append(node)
            for target in edge_map.get(node, []):
                visit(target)

        # Start from __start__ if present
        if "__start__" in visible_nodes:
            visit("__start__")

        # Add any remaining nodes
        for node in visible_nodes:
            if node not in visited:
                visit(node)

        # Build ASCII diagram
        lines: list[str] = []
        max_name_len = max(
            (
                len(n.replace("__", "").upper() if n.startswith("__") else n)
                for n in ordered_nodes
            ),
            default=7,
        )
        box_width = max(max_name_len + 4, 11)  # Minimum width of 11

        for i, node_name in enumerate(ordered_nodes):
            # Determine display name
            if node_name == "__start__":
                display_name = "START"
            elif node_name == "__end__":
                display_name = "END"
            else:
                display_name = node_name

            # Determine status
            if node_name == "__start__":
                status_symbol = "✓"
                status_text = ""
            elif node_name == current_node:
                status_symbol = "▶"
                status_text = " INTERRUPTED" if is_interrupted else " RUNNING"
            elif node_name in completed_nodes:
                status_symbol = "✓"
                status_text = ""
            else:
                status_symbol = "○"
                status_text = ""

            # Calculate padding for centering
            name_padding = (box_width - 2 - len(display_name)) // 2
            name_line = (
                "│"
                + " " * name_padding
                + display_name
                + " " * (box_width - 2 - name_padding - len(display_name))
                + "│"
            )

            # Build box
            lines.append("┌" + "─" * (box_width - 2) + "┐")
            lines.append(f"{name_line} {status_symbol}{status_text}")
            if i < len(ordered_nodes) - 1:
                # Add connector to next node
                connector_padding = (box_width - 2) // 2
                lines.append(
                    "└"
                    + "─" * connector_padding
                    + "┬"
                    + "─" * (box_width - 3 - connector_padding)
                    + "┘"
                )
                lines.append(" " * (connector_padding + 1) + "│")
                lines.append(" " * (connector_padding + 1) + "▼")
            else:
                lines.append("└" + "─" * (box_width - 2) + "┘")

        # Add legend
        if show_legend:
            lines.append("")
            lines.append("Legend: ✓ completed  ▶ current/interrupted  ○ pending")

        return "\n".join(lines)
