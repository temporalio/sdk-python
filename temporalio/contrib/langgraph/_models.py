"""Dataclass models for LangGraph-Temporal integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


def _coerce_to_message(value: Any) -> Any:
    """Coerce a dict to a LangChain message if it has a message type."""
    if isinstance(value, dict) and "type" in value:
        msg_type = value.get("type")
        if msg_type in (
            "human",
            "ai",
            "system",
            "function",
            "tool",
            "HumanMessageChunk",
            "AIMessageChunk",
            "SystemMessageChunk",
            "FunctionMessageChunk",
            "ToolMessageChunk",
            "chat",
            "ChatMessageChunk",
        ):
            # Use LangChain's AnyMessage type adapter to deserialize
            from langchain_core.messages import AnyMessage
            from pydantic import TypeAdapter

            return TypeAdapter(AnyMessage).validate_python(value)
    return value


def _coerce_state_values(state: dict[str, Any]) -> dict[str, Any]:
    """Coerce state dict values to LangChain message types where applicable."""
    result: dict[str, Any] = {}
    for key, value in state.items():
        if isinstance(value, list):
            result[key] = [_coerce_to_message(item) for item in value]
        else:
            result[key] = _coerce_to_message(value)
    return result


# ==============================================================================
# Store Models
# ==============================================================================


@dataclass
class StoreItem:
    """A key-value pair within a namespace."""

    namespace: tuple[str, ...]
    """Hierarchical namespace tuple."""

    key: str
    """The key within the namespace."""

    value: dict[str, Any]
    """The stored value."""


@dataclass
class StoreWrite:
    """A store write operation (put or delete)."""

    operation: Literal["put", "delete"]
    """The type of operation."""

    namespace: tuple[str, ...]
    """Hierarchical namespace tuple."""

    key: str
    """The key within the namespace."""

    value: dict[str, Any] | None = None
    """The value to store (None for delete operations)."""


@dataclass
class StoreSnapshot:
    """Snapshot of store data passed to an activity."""

    items: list[StoreItem] = field(default_factory=list)
    """List of store items in the snapshot."""


# ==============================================================================
# Channel Write Models
# ==============================================================================


def _is_langchain_message(value: Any) -> bool:
    """Check if value is a LangChain message."""
    try:
        from langchain_core.messages import BaseMessage

        return isinstance(value, BaseMessage)
    except ImportError:
        return False


def _is_langchain_message_list(value: Any) -> bool:
    """Check if value is a list of LangChain messages."""
    if not isinstance(value, list) or not value:
        return False
    return _is_langchain_message(value[0])


@dataclass
class ChannelWrite:
    """A write to a LangGraph channel with type preservation for messages."""

    channel: str
    """The channel name."""

    value: Any
    """The value to write."""

    value_type: str | None = None
    """Type hint for value reconstruction ('message' or 'message_list')."""

    @classmethod
    def create(cls, channel: str, value: Any) -> ChannelWrite:
        """Create a ChannelWrite, auto-detecting LangChain message types."""
        value_type = None
        if _is_langchain_message(value):
            value_type = "message"
        elif _is_langchain_message_list(value):
            value_type = "message_list"

        return cls(channel=channel, value=value, value_type=value_type)

    def reconstruct_value(self) -> Any:
        """Reconstruct the value, converting dicts back to LangChain messages."""
        if self.value_type == "message" and isinstance(self.value, dict):
            return _coerce_to_message(self.value)
        elif self.value_type == "message_list" and isinstance(self.value, list):
            return [
                _coerce_to_message(item) if isinstance(item, dict) else item
                for item in self.value
            ]
        return self.value

    def to_tuple(self) -> tuple[str, Any]:
        """Convert to (channel, value) tuple with reconstructed value."""
        return (self.channel, self.reconstruct_value())


@dataclass
class NodeActivityInput:
    """Input for the node execution activity."""

    node_name: str
    """Name of the node to execute."""

    task_id: str
    """Unique task ID from PregelExecutableTask."""

    graph_id: str
    """Graph ID for registry lookup."""

    input_state: dict[str, Any]
    """State to pass to node (coerced to LangChain messages on deserialization)."""

    config: dict[str, Any]
    """Filtered RunnableConfig."""

    path: tuple[str | int, ...]
    """Graph hierarchy path."""

    triggers: list[str]
    """List of channels that triggered this node."""

    resume_value: Any | None = None
    """Value to resume with (for interrupt handling)."""

    store_snapshot: StoreSnapshot | None = None
    """Snapshot of store data for the activity."""

    def __post_init__(self) -> None:
        """Coerce state values to LangChain messages after deserialization."""
        self.input_state = _coerce_state_values(self.input_state)


@dataclass
class InterruptValue:
    """Data about an interrupt raised by a node."""

    value: Any
    """The interrupt value."""

    node_name: str
    """Name of the node that raised the interrupt."""

    task_id: str
    """Task ID of the interrupted execution."""


@dataclass
class SendPacket:
    """Serializable representation of a LangGraph Send object."""

    node: str
    """Target node name."""

    arg: dict[str, Any]
    """Arguments to pass to the node."""

    @classmethod
    def from_send(cls, send: Any) -> SendPacket:
        """Create a SendPacket from a LangGraph Send object."""
        return cls(node=send.node, arg=send.arg)


@dataclass
class NodeActivityOutput:
    """Output from the node execution activity."""

    writes: list[ChannelWrite]
    """List of channel writes from the node."""

    interrupt: InterruptValue | None = None
    """Interrupt data if the node raised an interrupt."""

    store_writes: list[StoreWrite] = field(default_factory=list)
    """List of store write operations."""

    send_packets: list[SendPacket] = field(default_factory=list)
    """List of Send packets for dynamic node dispatch."""

    def to_write_tuples(self) -> list[tuple[str, Any]]:
        """Convert writes to (channel, value) tuples."""
        return [write.to_tuple() for write in self.writes]


@dataclass
class StateSnapshot:
    """Snapshot of graph execution state for checkpointing and continue-as-new."""

    values: dict[str, Any]
    """Current state values."""

    next: tuple[str, ...]
    """Next nodes to execute (empty if complete)."""

    metadata: dict[str, Any]
    """Execution metadata (step, completed_nodes, etc.)."""

    tasks: tuple[dict[str, Any], ...]
    """Pending tasks/interrupts."""

    store_state: list[dict[str, Any]] = field(default_factory=list)
    """Serialized store data."""


