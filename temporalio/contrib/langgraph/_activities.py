"""Temporal activities for LangGraph node execution.

This module provides the activity that executes LangGraph nodes within
Temporal workflows. The activity retrieves the graph from the registry,
looks up the node, executes it, and captures the writes.
"""

from __future__ import annotations

import asyncio
import warnings
from collections import deque
from typing import TYPE_CHECKING, Any, Sequence, cast

from temporalio import activity

from temporalio.contrib.langgraph._graph_registry import get_graph
from temporalio.contrib.langgraph._models import (
    ChannelWrite,
    InterruptValue,
    NodeActivityInput,
    NodeActivityOutput,
    StoreSnapshot,
)
from temporalio.contrib.langgraph._store import ActivityLocalStore

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

# Import CONFIG_KEY_SEND and CONFIG_KEY_READ for Pregel context injection
# CONFIG_KEY_SEND is for write capture, CONFIG_KEY_READ is for state reading
# CONFIG_KEY_SCRATCHPAD is needed for interrupt() to work
# CONFIG_KEY_RUNTIME is for injecting the runtime with store access
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from langgraph.constants import CONFIG_KEY_SEND
    from langgraph._internal._constants import (
        CONFIG_KEY_CHECKPOINT_NS,
        CONFIG_KEY_READ,
        CONFIG_KEY_RUNTIME,
        CONFIG_KEY_SCRATCHPAD,
    )
    from langgraph._internal._scratchpad import PregelScratchpad
    from langgraph.errors import GraphInterrupt as LangGraphInterrupt
    from langgraph.runtime import Runtime


@activity.defn(name="execute_langgraph_node")
async def execute_node(input_data: NodeActivityInput) -> NodeActivityOutput:
    """Execute a LangGraph node as a Temporal activity.

    This activity:
    1. Retrieves the cached graph from the registry
    2. Looks up the node by name
    3. Executes the node with the provided state
    4. Captures writes via CONFIG_KEY_SEND callback
    5. Returns writes wrapped in ChannelWrite for type preservation

    The activity uses heartbeats to report progress during execution.

    Args:
        input_data: The input data containing node name, graph ID, state, etc.

    Returns:
        NodeActivityOutput containing the writes produced by the node.

    Raises:
        ValueError: If the node is not found in the graph.
        Exception: Any exception raised by the node during execution.
    """
    # Get cached graph from registry
    graph = get_graph(input_data.graph_id)

    # Get node
    pregel_node = graph.nodes.get(input_data.node_name)
    if pregel_node is None:
        available = list(graph.nodes.keys())
        raise ValueError(
            f"Node '{input_data.node_name}' not found in graph "
            f"'{input_data.graph_id}'. Available nodes: {available}"
        )

    # Get the node's runnable
    node_runnable = pregel_node.node
    if node_runnable is None:
        return NodeActivityOutput(writes=[])

    # Setup write capture deque
    # Writers in LangGraph call CONFIG_KEY_SEND callback with list of (channel, value) tuples
    writes: deque[tuple[str, Any]] = deque()

    # Create state reader function for CONFIG_KEY_READ
    # This allows conditional edges and ChannelRead to access current state
    # The reader returns a merged view: input_state + captured writes
    # This is critical for conditional edges where the routing function
    # needs to see writes from the node that just executed
    base_state = input_data.input_state

    def read_state(
        channel: str | Sequence[str], fresh: bool = False
    ) -> Any | dict[str, Any]:
        """Read state from input_state dict merged with captured writes.

        This mimics the Pregel channel read behavior for activity execution.
        The merged view allows routing functions to see writes from the
        node function that just executed.
        """
        # Build a dict of the latest writes (later writes override earlier ones)
        write_values: dict[str, Any] = {}
        for ch, val in writes:
            write_values[ch] = val

        if isinstance(channel, str):
            # Return write value if present, otherwise base state
            if channel in write_values:
                return write_values[channel]
            return base_state.get(channel)
        else:
            # Return merged dict for multiple channels
            result: dict[str, Any] = {}
            for k in channel:
                if k in write_values:
                    result[k] = write_values[k]
                else:
                    result[k] = base_state.get(k)
            return result

    # Build config with Pregel context callbacks injected
    # CONFIG_KEY_SEND is REQUIRED for capturing writes
    # CONFIG_KEY_READ is REQUIRED for conditional edges and state reading
    # CONFIG_KEY_SCRATCHPAD is REQUIRED for interrupt() to work
    #
    # PregelScratchpad tracks interrupt state:
    # - resume: list of resume values (consumed in order by interrupt() calls)
    # - interrupt_counter: returns index of current interrupt
    # - get_null_resume: returns None or raises for missing resume values
    #
    # When resuming, we provide the resume value in the resume list.
    # interrupt() will pop from this list and return the value instead of raising.
    resume_values: list[Any] = []
    if input_data.resume_value is not None:
        resume_values = [input_data.resume_value]

    # Track interrupt index for matching resume values to interrupts
    interrupt_idx = 0

    def interrupt_counter() -> int:
        nonlocal interrupt_idx
        idx = interrupt_idx
        interrupt_idx += 1
        return idx

    def get_null_resume(consume: bool) -> Any:
        # Called when interrupt() doesn't have a resume value
        # Return None to signal no resume value available
        return None

    scratchpad = PregelScratchpad(
        step=0,
        stop=1,
        call_counter=lambda: 0,
        interrupt_counter=interrupt_counter,
        get_null_resume=get_null_resume,
        resume=resume_values,
        subgraph_counter=lambda: 0,
    )

    # Create activity-local store if snapshot provided
    store: ActivityLocalStore | None = None
    if input_data.store_snapshot is not None:
        store = ActivityLocalStore(input_data.store_snapshot)

    configurable: dict[str, Any] = {
        **input_data.config.get("configurable", {}),
        CONFIG_KEY_SEND: writes.extend,  # Callback to capture writes
        CONFIG_KEY_READ: read_state,  # Callback to read state
        CONFIG_KEY_SCRATCHPAD: scratchpad,  # Scratchpad for interrupt handling
        CONFIG_KEY_CHECKPOINT_NS: "",  # Namespace for checkpointing (used by interrupt)
    }

    # Inject store via Runtime if available
    # LangGraph's get_store() accesses store through config[configurable][__pregel_runtime].store
    if store is not None:
        runtime = Runtime(store=store)
        configurable[CONFIG_KEY_RUNTIME] = runtime

    config: dict[str, Any] = {
        **input_data.config,
        "configurable": configurable,
    }

    # Send heartbeat indicating execution start
    activity.heartbeat(
        {
            "node": input_data.node_name,
            "task_id": input_data.task_id,
            "graph_id": input_data.graph_id,
            "status": "executing",
        }
    )

    # Execute the node
    # The node_runnable includes the bound function and writers
    # Cast config to RunnableConfig for type checking
    runnable_config = cast("RunnableConfig", config)
    try:
        if asyncio.iscoroutinefunction(
            getattr(node_runnable, "ainvoke", None)
        ) or asyncio.iscoroutinefunction(getattr(node_runnable, "invoke", None)):
            result = await node_runnable.ainvoke(input_data.input_state, runnable_config)
        else:
            result = node_runnable.invoke(input_data.input_state, runnable_config)
    except LangGraphInterrupt as e:
        # Node called interrupt() - return interrupt data instead of writes
        activity.heartbeat(
            {
                "node": input_data.node_name,
                "task_id": input_data.task_id,
                "graph_id": input_data.graph_id,
                "status": "interrupted",
            }
        )
        # Extract the value passed to interrupt()
        # GraphInterrupt contains a tuple of Interrupt objects in args[0]
        # Each Interrupt has a .value attribute with the actual interrupt value
        interrupt_value = None
        if e.args and len(e.args) > 0:
            interrupts = e.args[0]
            if interrupts and len(interrupts) > 0:
                # Get the value from the first Interrupt object
                interrupt_value = interrupts[0].value
        # Collect store writes even on interrupt
        store_writes = store.get_writes() if store is not None else []
        return NodeActivityOutput(
            writes=[],
            interrupt=InterruptValue(
                value=interrupt_value,
                node_name=input_data.node_name,
                task_id=input_data.task_id,
            ),
            store_writes=store_writes,
        )
    except Exception:
        # Send heartbeat indicating failure before re-raising
        activity.heartbeat(
            {
                "node": input_data.node_name,
                "task_id": input_data.task_id,
                "graph_id": input_data.graph_id,
                "status": "failed",
            }
        )
        raise

    # Note: Writes are primarily captured via CONFIG_KEY_SEND callback above.
    # The callback is invoked by LangGraph's internal writer mechanism.
    # For nodes that return dicts directly (without using writers),
    # we also check the result as a fallback.
    if isinstance(result, dict) and not writes:
        # Only use result if CONFIG_KEY_SEND didn't capture anything
        for channel, value in result.items():
            writes.append((channel, value))

    # Send heartbeat indicating completion
    activity.heartbeat(
        {
            "node": input_data.node_name,
            "task_id": input_data.task_id,
            "graph_id": input_data.graph_id,
            "status": "completed",
            "writes_count": len(writes),
        }
    )

    # Convert writes to ChannelWrite for type preservation
    channel_writes = [
        ChannelWrite.create(channel, value) for channel, value in writes
    ]

    # Collect store writes
    store_writes = store.get_writes() if store is not None else []

    return NodeActivityOutput(writes=channel_writes, store_writes=store_writes)
