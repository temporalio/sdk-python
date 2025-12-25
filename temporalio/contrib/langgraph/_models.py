"""Pydantic models for LangGraph-Temporal integration.

These models handle serialization of node activity inputs and outputs,
with special handling for LangChain message types.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


def _reconstruct_message(data: dict[str, Any]) -> Any:
    """Reconstruct a LangChain message from a serialized dict.

    LangChain messages include a 'type' field that identifies the message class.
    """
    from langchain_core.messages import (
        AIMessage,
        FunctionMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )

    message_type = data.get("type", "")
    message_map: dict[str, type] = {
        "human": HumanMessage,
        "ai": AIMessage,
        "system": SystemMessage,
        "function": FunctionMessage,
        "tool": ToolMessage,
    }

    message_class = message_map.get(message_type)
    if message_class:
        # Remove 'type' field as it's not a constructor argument
        data_copy = {k: v for k, v in data.items() if k != "type"}
        return message_class(**data_copy)

    # Return as-is if unknown type
    return data


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
            return _reconstruct_message(self.value)
        elif self.value_type == "message_list" and isinstance(self.value, list):
            return [
                _reconstruct_message(item) if isinstance(item, dict) else item
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
    input_state: dict[str, Any]
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
