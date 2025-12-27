"""Pydantic models for LangGraph-Temporal integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict

if TYPE_CHECKING:
    pass


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


# Type alias for state dict with automatic message coercion
LangGraphState = Annotated[dict[str, Any], BeforeValidator(_coerce_state_values)]


# ==============================================================================
# Store Models
# ==============================================================================


class StoreItem(BaseModel):
    """A key-value pair within a namespace."""

    namespace: tuple[str, ...]
    key: str
    value: dict[str, Any]


class StoreWrite(BaseModel):
    """A store write operation (put or delete)."""

    operation: Literal["put", "delete"]
    namespace: tuple[str, ...]
    key: str
    value: Optional[dict[str, Any]] = None


class StoreSnapshot(BaseModel):
    """Snapshot of store data passed to an activity."""

    items: list[StoreItem] = []


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


class ChannelWrite(BaseModel):
    """A write to a LangGraph channel with type preservation for messages."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    channel: str
    value: Any
    value_type: str | None = None

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


class NodeActivityInput(BaseModel):
    """Input for the node execution activity."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    node_name: str
    task_id: str
    graph_id: str
    input_state: LangGraphState
    config: dict[str, Any]
    path: tuple[str | int, ...]
    triggers: list[str]
    resume_value: Optional[Any] = None
    store_snapshot: Optional[StoreSnapshot] = None


class InterruptValue(BaseModel):
    """Data about an interrupt raised by a node."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    value: Any
    node_name: str
    task_id: str


class SendPacket(BaseModel):
    """Serializable representation of a LangGraph Send object."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    node: str
    arg: dict[str, Any]

    @classmethod
    def from_send(cls, send: Any) -> "SendPacket":
        """Create a SendPacket from a LangGraph Send object."""
        return cls(node=send.node, arg=send.arg)


class NodeActivityOutput(BaseModel):
    """Output from the node execution activity."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    writes: list[ChannelWrite]
    interrupt: Optional[InterruptValue] = None
    store_writes: list[StoreWrite] = []
    send_packets: list[SendPacket] = []

    def to_write_tuples(self) -> list[tuple[str, Any]]:
        """Convert writes to (channel, value) tuples."""
        return [write.to_tuple() for write in self.writes]


class StateSnapshot(BaseModel):
    """Snapshot of graph execution state for checkpointing and continue-as-new."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    values: dict[str, Any]
    """Current state values."""

    next: tuple[str, ...]
    """Next nodes to execute (empty if complete)."""

    metadata: dict[str, Any]
    """Execution metadata (step, completed_nodes, etc.)."""

    tasks: tuple[dict[str, Any], ...]
    """Pending tasks/interrupts."""

    store_state: list[dict[str, Any]] = []
    """Serialized store data."""


# ==============================================================================
# Tool Activity Models
# ==============================================================================


class ToolActivityInput(BaseModel):
    """Input for the tool execution activity."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_name: str
    tool_input: Any


class ToolActivityOutput(BaseModel):
    """Output from the tool execution activity."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    output: Any


# ==============================================================================
# Chat Model Activity Models
# ==============================================================================


class ChatModelActivityInput(BaseModel):
    """Input for the chat model execution activity."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_name: Optional[str]
    messages: list[dict[str, Any]]
    stop: Optional[list[str]] = None
    kwargs: dict[str, Any] = {}


class ChatGenerationData(BaseModel):
    """Serialized chat generation data."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    message: dict[str, Any]
    generation_info: Optional[dict[str, Any]] = None


class ChatModelActivityOutput(BaseModel):
    """Output from the chat model execution activity."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    generations: list[dict[str, Any]]
    llm_output: Optional[dict[str, Any]] = None
