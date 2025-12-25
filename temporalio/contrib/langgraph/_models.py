"""Pydantic models for LangGraph-Temporal integration.

These models handle serialization of node activity inputs and outputs,
with proper type handling for LangChain message types via Pydantic's
discriminated unions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage


def _coerce_to_message(value: Any) -> Any:
    """Coerce a dict to a LangChain message if it looks like one.

    This validator enables automatic deserialization of LangChain messages
    when they are stored in dict[str, Any] fields.
    """
    if isinstance(value, dict) and "type" in value:
        msg_type = value.get("type")
        if msg_type in ("human", "ai", "system", "function", "tool",
                        "HumanMessageChunk", "AIMessageChunk", "SystemMessageChunk",
                        "FunctionMessageChunk", "ToolMessageChunk", "chat", "ChatMessageChunk"):
            # Use LangChain's AnyMessage type adapter to deserialize
            from langchain_core.messages import AnyMessage
            from pydantic import TypeAdapter
            return TypeAdapter(AnyMessage).validate_python(value)
    return value


def _coerce_state_values(state: dict[str, Any]) -> dict[str, Any]:
    """Coerce state dict values, converting message dicts to proper types."""
    result: dict[str, Any] = {}
    for key, value in state.items():
        if isinstance(value, list):
            result[key] = [_coerce_to_message(item) for item in value]
        else:
            result[key] = _coerce_to_message(value)
    return result


# Type alias for state dict with automatic message coercion
LangGraphState = Annotated[dict[str, Any], BeforeValidator(_coerce_state_values)]


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
    """Represents a write to a LangGraph channel with type preservation.

    This model preserves type information for LangChain messages during
    Temporal serialization. When values are serialized through Temporal's
    payload converter, Pydantic models in `Any` typed fields lose their
    type information. This class records the value type and enables
    reconstruction after deserialization.

    Attributes:
        channel: The name of the channel being written to.
        value: The value being written (may be a message or any other type).
        value_type: Type hint for reconstruction ("message", "message_list", or None).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    channel: str
    value: Any
    value_type: str | None = None

    @classmethod
    def create(cls, channel: str, value: Any) -> ChannelWrite:
        """Factory method that automatically detects LangChain message types.

        Args:
            channel: The channel name.
            value: The value to write.

        Returns:
            A ChannelWrite instance with appropriate value_type set.
        """
        value_type = None
        if _is_langchain_message(value):
            value_type = "message"
        elif _is_langchain_message_list(value):
            value_type = "message_list"

        return cls(channel=channel, value=value, value_type=value_type)

    def reconstruct_value(self) -> Any:
        """Reconstruct the value, converting dicts back to LangChain messages.

        Returns:
            The reconstructed value with proper message types.
        """
        if self.value_type == "message" and isinstance(self.value, dict):
            return _coerce_to_message(self.value)
        elif self.value_type == "message_list" and isinstance(self.value, list):
            return [
                _coerce_to_message(item) if isinstance(item, dict) else item
                for item in self.value
            ]
        return self.value

    def to_tuple(self) -> tuple[str, Any]:
        """Convert to (channel, value) tuple with reconstructed value.

        Returns:
            A tuple of (channel_name, reconstructed_value).
        """
        return (self.channel, self.reconstruct_value())


class NodeActivityInput(BaseModel):
    """Input data for the node execution activity.

    This model encapsulates all data needed to execute a LangGraph node
    in a Temporal activity.

    Attributes:
        node_name: Name of the node to execute.
        task_id: Unique identifier for this task execution.
        graph_id: ID of the graph in the plugin registry.
        input_state: The state to pass to the node.
        config: Filtered RunnableConfig (without internal keys).
        path: Graph hierarchy path for nested graphs.
        triggers: List of channels that triggered this task.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    node_name: str
    task_id: str
    graph_id: str
    input_state: LangGraphState  # Auto-coerces message dicts to LangChain messages
    config: dict[str, Any]
    path: tuple[str | int, ...]
    triggers: list[str]


class NodeActivityOutput(BaseModel):
    """Output data from the node execution activity.

    Attributes:
        writes: List of channel writes produced by the node.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    writes: list[ChannelWrite]

    def to_write_tuples(self) -> list[tuple[str, Any]]:
        """Convert writes to (channel, value) tuples.

        Returns:
            List of (channel_name, reconstructed_value) tuples.
        """
        return [write.to_tuple() for write in self.writes]
