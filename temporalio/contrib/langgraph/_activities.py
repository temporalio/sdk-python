"""Temporal activities for LangGraph node execution."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import TYPE_CHECKING, Any, Sequence, cast

from temporalio import activity

logger = logging.getLogger(__name__)

from temporalio.contrib.langgraph._exceptions import node_not_found_error
from temporalio.contrib.langgraph._graph_registry import get_graph
from temporalio.contrib.langgraph._models import (
    ChannelWrite,
    ChatModelActivityInput,
    ChatModelActivityOutput,
    InterruptValue,
    NodeActivityInput,
    NodeActivityOutput,
    StoreSnapshot,
    ToolActivityInput,
    ToolActivityOutput,
)
from temporalio.contrib.langgraph._store import ActivityLocalStore

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig

# =============================================================================
# LangGraph Internal API Usage
# =============================================================================
#
# This module uses LangGraph internal APIs (langgraph._internal.*) because we
# execute individual graph nodes as separate Temporal activities, outside of
# LangGraph's normal Pregel execution loop.
#
# WHY WE NEED THESE:
# LangGraph's Pregel executor injects special config keys when running nodes:
#
# - CONFIG_KEY_SEND: Callback to capture node outputs (writes to channels)
# - CONFIG_KEY_READ: Callback to read current state (for conditional edges)
# - CONFIG_KEY_SCRATCHPAD: Tracks interrupt state for interrupt() to work
# - CONFIG_KEY_RUNTIME: Provides store access and other runtime services
# - CONFIG_KEY_CHECKPOINT_NS: Namespace for checkpoint operations
# - PregelScratchpad: Class that manages interrupt/resume state
#
# Since we run nodes individually in activities, we must inject this same
# context to make nodes behave as if they're running inside Pregel.
#
# RISKS:
# These are private APIs that may change in future LangGraph versions.
# If LangGraph changes these, this integration will need updates.
#
# ALTERNATIVES CONSIDERED:
# - Defining our own string constants: Fragile if LangGraph changes values
# - Running entire graph in one activity: Loses per-node retry/timeout control
# - Requesting public API from LangGraph: Best long-term, but uncertain timeline
#
# =============================================================================

from langgraph._internal._constants import (
    CONFIG_KEY_CHECKPOINT_NS,
    CONFIG_KEY_READ,
    CONFIG_KEY_RUNTIME,
    CONFIG_KEY_SCRATCHPAD,
    CONFIG_KEY_SEND,
)
from langgraph._internal._scratchpad import PregelScratchpad
from langgraph.errors import GraphInterrupt as LangGraphInterrupt
from langgraph.runtime import Runtime
from langgraph.types import Send


async def _execute_node_impl(input_data: NodeActivityInput) -> NodeActivityOutput:
    """Shared implementation for node execution activities."""
    logger.debug(
        "Executing node %s in graph %s",
        input_data.node_name,
        input_data.graph_id,
    )

    # Get cached graph from registry
    graph = get_graph(input_data.graph_id)

    # Get node
    pregel_node = graph.nodes.get(input_data.node_name)
    if pregel_node is None:
        available = list(graph.nodes.keys())
        raise node_not_found_error(input_data.node_name, input_data.graph_id, available)

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

    # Create activity-local store for node execution
    # Always create a store so get_store() works, even on first invocation with no data
    store_snapshot = input_data.store_snapshot or StoreSnapshot(items=[])
    store = ActivityLocalStore(store_snapshot)

    configurable: dict[str, Any] = {
        **input_data.config.get("configurable", {}),
        CONFIG_KEY_SEND: writes.extend,  # Callback to capture writes
        CONFIG_KEY_READ: read_state,  # Callback to read state
        CONFIG_KEY_SCRATCHPAD: scratchpad,  # Scratchpad for interrupt handling
        CONFIG_KEY_CHECKPOINT_NS: "",  # Namespace for checkpointing (used by interrupt)
    }

    # Inject store via Runtime
    # LangGraph's get_store() accesses store through config[configurable][__pregel_runtime].store
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
            result = await node_runnable.ainvoke(
                input_data.input_state, runnable_config
            )
        else:
            result = node_runnable.invoke(input_data.input_state, runnable_config)
    except LangGraphInterrupt as e:
        # Node called interrupt() - return interrupt data instead of writes
        logger.debug(
            "Node %s in graph %s raised interrupt",
            input_data.node_name,
            input_data.graph_id,
        )
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
        store_writes = store.get_writes()
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
        logger.debug(
            "Node %s in graph %s failed with exception",
            input_data.node_name,
            input_data.graph_id,
            exc_info=True,
        )
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

    # Separate Send objects from regular channel writes
    # Send objects are control flow instructions that need to go back to the
    # Pregel loop in the workflow to create new tasks
    from temporalio.contrib.langgraph._models import SendPacket

    # Convert writes to ChannelWrite, capturing Send objects separately
    channel_writes = []
    send_packets = []
    for channel, value in writes:
        if isinstance(value, Send):
            send_packets.append(SendPacket.from_send(value))
        else:
            channel_writes.append(ChannelWrite.create(channel, value))

    # Collect store writes
    store_writes = store.get_writes()

    logger.debug(
        "Node %s in graph %s completed with %d writes",
        input_data.node_name,
        input_data.graph_id,
        len(channel_writes),
    )

    return NodeActivityOutput(
        writes=channel_writes,
        store_writes=store_writes,
        send_packets=send_packets,
    )


@activity.defn
async def langgraph_node(input_data: NodeActivityInput) -> NodeActivityOutput:
    """Execute a LangGraph node as a Temporal activity."""
    return await _execute_node_impl(input_data)


@activity.defn
async def resume_langgraph_node(input_data: NodeActivityInput) -> NodeActivityOutput:
    """Resume an interrupted LangGraph node as a Temporal activity."""
    return await _execute_node_impl(input_data)


@activity.defn(name="execute_langgraph_tool")
async def execute_tool(
    input_data: ToolActivityInput,
) -> ToolActivityOutput:
    """Execute a LangChain tool as a Temporal activity."""
    logger.debug("Executing tool %s", input_data.tool_name)

    from temporalio.contrib.langgraph._tool_registry import get_tool

    # Get tool from registry
    tool = get_tool(input_data.tool_name)

    # Execute the tool
    # Tools can accept various input formats
    result = await tool.ainvoke(input_data.tool_input)

    logger.debug("Tool %s completed", input_data.tool_name)

    return ToolActivityOutput(output=result)


@activity.defn(name="execute_langgraph_chat_model")
async def execute_chat_model(
    input_data: ChatModelActivityInput,
) -> ChatModelActivityOutput:
    """Execute a LangChain chat model call as a Temporal activity."""
    model_name = input_data.model_name or "default"
    logger.debug("Executing chat model %s with %d messages", model_name, len(input_data.messages))

    from langchain_core.messages import AnyMessage
    from pydantic import TypeAdapter

    from temporalio.contrib.langgraph._model_registry import get_model

    # Get model from registry
    model = get_model(model_name)

    # Deserialize messages
    messages: list[Any] = []
    for msg_dict in input_data.messages:
        # Use LangChain's message type adapter for proper deserialization
        deserialized_msg: Any = TypeAdapter(AnyMessage).validate_python(msg_dict)
        messages.append(deserialized_msg)

    # Execute the model
    # Use _agenerate for direct access to ChatResult
    result = await model._agenerate(
        messages,
        stop=input_data.stop,
        **input_data.kwargs,
    )

    # Serialize generations for return
    generations = []
    for gen in result.generations:
        gen_data = {
            "message": gen.message.model_dump()
            if hasattr(gen.message, "model_dump")
            else {"content": str(gen.message.content), "type": "ai"},
            "generation_info": gen.generation_info,
        }
        generations.append(gen_data)

    logger.debug("Chat model %s completed with %d generations", model_name, len(generations))

    return ChatModelActivityOutput(
        generations=generations,
        llm_output=result.llm_output,
    )
