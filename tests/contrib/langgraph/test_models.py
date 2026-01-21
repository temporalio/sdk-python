"""Unit tests for LangGraph Pydantic models.

Tests for ChannelWrite, NodeActivityInput/Output, StoreItem, StoreWrite,
StoreSnapshot, InterruptValue, and StateSnapshot models.
"""

from __future__ import annotations


class TestChannelWrite:
    """Tests for ChannelWrite model."""

    def test_channel_write_basic(self) -> None:
        """ChannelWrite should store channel and value."""
        from temporalio.contrib.langgraph._models import ChannelWrite

        write = ChannelWrite(channel="output", value=42)
        assert write.channel == "output"
        assert write.value == 42
        assert write.value_type is None

    def test_channel_write_create_detects_message(self) -> None:
        """ChannelWrite.create should detect LangChain messages."""
        from langchain_core.messages import HumanMessage

        from temporalio.contrib.langgraph._models import ChannelWrite

        msg = HumanMessage(content="Hello")
        write = ChannelWrite.create("messages", msg)

        assert write.channel == "messages"
        assert write.value_type == "message"

    def test_channel_write_create_detects_message_list(self) -> None:
        """ChannelWrite.create should detect list of messages."""
        from langchain_core.messages import AIMessage, HumanMessage

        from temporalio.contrib.langgraph._models import ChannelWrite

        messages = [HumanMessage(content="Hi"), AIMessage(content="Hello")]
        write = ChannelWrite.create("messages", messages)

        assert write.value_type == "message_list"

    def test_channel_write_create_regular_value(self) -> None:
        """ChannelWrite.create should handle regular values."""
        from temporalio.contrib.langgraph._models import ChannelWrite

        write = ChannelWrite.create("count", 10)

        assert write.channel == "count"
        assert write.value == 10
        assert write.value_type is None

    def test_channel_write_reconstruct_message(self) -> None:
        """ChannelWrite should reconstruct messages from dicts."""
        from temporalio.contrib.langgraph._models import ChannelWrite

        # Simulate serialized message (as dict)
        serialized = {"content": "Hello", "type": "human"}
        write = ChannelWrite(channel="messages", value=serialized, value_type="message")

        reconstructed = write.reconstruct_value()
        assert reconstructed.content == "Hello"
        assert type(reconstructed).__name__ == "HumanMessage"

    def test_channel_write_to_tuple(self) -> None:
        """ChannelWrite.to_tuple should return (channel, value)."""
        from temporalio.contrib.langgraph._models import ChannelWrite

        write = ChannelWrite(channel="output", value="result")
        assert write.to_tuple() == ("output", "result")


class TestNodeActivityInput:
    """Tests for NodeActivityInput model."""

    def test_node_activity_input(self) -> None:
        """NodeActivityInput should store all required fields."""
        from temporalio.contrib.langgraph._models import NodeActivityInput

        input_data = NodeActivityInput(
            node_name="my_node",
            task_id="task_123",
            graph_id="my_graph",
            input_state={"value": 1},
            config={"key": "value"},
            path=("graph", "subgraph"),
            triggers=["input"],
        )

        assert input_data.node_name == "my_node"
        assert input_data.task_id == "task_123"
        assert input_data.graph_id == "my_graph"
        assert input_data.input_state == {"value": 1}

    def test_node_activity_input_with_store(self) -> None:
        """NodeActivityInput should include store_snapshot."""
        from temporalio.contrib.langgraph._models import (
            NodeActivityInput,
            StoreItem,
            StoreSnapshot,
        )

        snapshot = StoreSnapshot(
            items=[StoreItem(namespace=("user",), key="k", value={"v": 1})]
        )
        input_data = NodeActivityInput(
            node_name="my_node",
            task_id="task_123",
            graph_id="my_graph",
            input_state={"value": 1},
            config={},
            path=tuple(),
            triggers=[],
            store_snapshot=snapshot,
        )
        assert input_data.store_snapshot is not None
        assert len(input_data.store_snapshot.items) == 1

    def test_node_activity_input_with_resume(self) -> None:
        """NodeActivityInput should support resume_value field."""
        from temporalio.contrib.langgraph._models import NodeActivityInput

        input_data = NodeActivityInput(
            node_name="my_node",
            task_id="task_123",
            graph_id="my_graph",
            input_state={"value": 1},
            config={},
            path=(),
            triggers=[],
            resume_value="user_response",
        )

        assert input_data.resume_value == "user_response"


class TestNodeActivityOutput:
    """Tests for NodeActivityOutput model."""

    def test_node_activity_output(self) -> None:
        """NodeActivityOutput should store writes."""
        from temporalio.contrib.langgraph._models import (
            ChannelWrite,
            NodeActivityOutput,
        )

        output = NodeActivityOutput(
            writes=[
                ChannelWrite(channel="a", value=1),
                ChannelWrite(channel="b", value=2),
            ]
        )

        assert len(output.writes) == 2
        tuples = output.to_write_tuples()
        assert tuples == [("a", 1), ("b", 2)]

    def test_node_activity_output_with_store_writes(self) -> None:
        """NodeActivityOutput should include store_writes."""
        from temporalio.contrib.langgraph._models import (
            NodeActivityOutput,
            StoreWrite,
        )

        output = NodeActivityOutput(
            writes=[],
            store_writes=[
                StoreWrite(
                    operation="put",
                    namespace=("user", "1"),
                    key="pref",
                    value={"v": 1},
                )
            ],
        )
        assert len(output.store_writes) == 1
        assert output.store_writes[0].operation == "put"

    def test_node_activity_output_with_interrupt(self) -> None:
        """NodeActivityOutput should support interrupt field."""
        from temporalio.contrib.langgraph._models import (
            InterruptValue,
            NodeActivityOutput,
        )

        output = NodeActivityOutput(
            writes=[],
            interrupt=InterruptValue(
                value="waiting",
                node_name="wait_node",
                task_id="task_789",
            ),
        )

        assert output.interrupt is not None
        assert output.interrupt.value == "waiting"
        assert len(output.writes) == 0


class TestStoreModels:
    """Tests for store-related models."""

    def test_store_item(self) -> None:
        """StoreItem should store namespace, key, value."""
        from temporalio.contrib.langgraph._models import StoreItem

        item = StoreItem(
            namespace=("user", "123"),
            key="preferences",
            value={"theme": "dark"},
        )
        assert item.namespace == ("user", "123")
        assert item.key == "preferences"
        assert item.value == {"theme": "dark"}

    def test_store_write_put(self) -> None:
        """StoreWrite should represent put operations."""
        from temporalio.contrib.langgraph._models import StoreWrite

        write = StoreWrite(
            operation="put",
            namespace=("user", "123"),
            key="settings",
            value={"notifications": True},
        )
        assert write.operation == "put"
        assert write.namespace == ("user", "123")
        assert write.key == "settings"
        assert write.value == {"notifications": True}

    def test_store_write_delete(self) -> None:
        """StoreWrite should represent delete operations."""
        from temporalio.contrib.langgraph._models import StoreWrite

        write = StoreWrite(
            operation="delete",
            namespace=("user", "123"),
            key="old_key",
        )
        assert write.operation == "delete"
        assert write.value is None

    def test_store_snapshot(self) -> None:
        """StoreSnapshot should contain list of store items."""
        from temporalio.contrib.langgraph._models import StoreItem, StoreSnapshot

        snapshot = StoreSnapshot(
            items=[
                StoreItem(namespace=("user", "1"), key="k1", value={"v": 1}),
                StoreItem(namespace=("user", "2"), key="k2", value={"v": 2}),
            ]
        )
        assert len(snapshot.items) == 2
        assert snapshot.items[0].key == "k1"


class TestInterruptValue:
    """Tests for InterruptValue model."""

    def test_interrupt_value_model(self) -> None:
        """InterruptValue should store interrupt data."""
        from temporalio.contrib.langgraph._models import InterruptValue

        interrupt = InterruptValue(
            value="Please confirm",
            node_name="confirm_node",
            task_id="task_456",
        )

        assert interrupt.value == "Please confirm"
        assert interrupt.node_name == "confirm_node"
        assert interrupt.task_id == "task_456"


class TestCommandOutput:
    """Tests for CommandOutput model used in ParentCommand handling."""

    def test_command_output_from_command_single_goto(self) -> None:
        """CommandOutput should convert single goto to list."""
        from unittest.mock import MagicMock

        from temporalio.contrib.langgraph._models import CommandOutput

        # Mock a Command object
        mock_command = MagicMock()
        mock_command.goto = "agent1"
        mock_command.update = {"messages": ["new msg"]}
        mock_command.resume = None

        output = CommandOutput.from_command(mock_command)

        assert output.goto == ["agent1"]
        assert output.update == {"messages": ["new msg"]}
        assert output.resume is None

    def test_command_output_from_command_list_goto(self) -> None:
        """CommandOutput should preserve list goto."""
        from unittest.mock import MagicMock

        from temporalio.contrib.langgraph._models import CommandOutput

        mock_command = MagicMock()
        mock_command.goto = ["agent1", "agent2", "agent3"]
        mock_command.update = {"value": 100}
        mock_command.resume = None

        output = CommandOutput.from_command(mock_command)

        assert output.goto == ["agent1", "agent2", "agent3"]
        assert output.update == {"value": 100}

    def test_command_output_from_command_no_goto(self) -> None:
        """CommandOutput should handle None goto."""
        from unittest.mock import MagicMock

        from temporalio.contrib.langgraph._models import CommandOutput

        mock_command = MagicMock()
        mock_command.goto = None
        mock_command.update = {"data": "value"}
        mock_command.resume = None

        output = CommandOutput.from_command(mock_command)

        assert output.goto == []
        assert output.update == {"data": "value"}

    def test_command_output_from_command_with_send_object(self) -> None:
        """CommandOutput should handle Send objects in goto."""
        from unittest.mock import MagicMock

        from temporalio.contrib.langgraph._models import CommandOutput

        # Mock Send object
        mock_send = MagicMock()
        mock_send.node = "tools"

        mock_command = MagicMock()
        mock_command.goto = [mock_send, "agent1"]
        mock_command.update = None
        mock_command.resume = None

        output = CommandOutput.from_command(mock_command)

        # Send objects should extract their node attribute
        assert output.goto == ["tools", "agent1"]


class TestSendPacket:
    """Tests for SendPacket model used in Send API handling."""

    def test_send_packet_from_send(self) -> None:
        """SendPacket should convert from langgraph Send object."""
        from unittest.mock import MagicMock

        from temporalio.contrib.langgraph._models import SendPacket

        # Mock a Send object
        mock_send = MagicMock()
        mock_send.node = "tools"
        mock_send.arg = {"messages": [], "tool_call": {"name": "calc"}}

        packet = SendPacket.from_send(mock_send)

        assert packet.node == "tools"
        assert packet.arg == {"messages": [], "tool_call": {"name": "calc"}}

    def test_send_packet_basic(self) -> None:
        """SendPacket should store node name and arg."""
        from temporalio.contrib.langgraph._models import SendPacket

        packet = SendPacket(node="agent", arg={"value": 42})

        assert packet.node == "agent"
        assert packet.arg == {"value": 42}


class TestNodeActivityOutputParentCommand:
    """Tests for NodeActivityOutput with parent_command field."""

    def test_output_with_parent_command(self) -> None:
        """NodeActivityOutput should store parent_command."""
        from temporalio.contrib.langgraph._models import (
            CommandOutput,
            NodeActivityOutput,
        )

        output = NodeActivityOutput(
            writes=[],
            parent_command=CommandOutput(
                update={"messages": ["test"]},
                goto=["agent1", "agent2"],
            ),
        )

        assert output.parent_command is not None
        assert output.parent_command.goto == ["agent1", "agent2"]
        assert output.parent_command.update == {"messages": ["test"]}

    def test_output_without_parent_command(self) -> None:
        """NodeActivityOutput should default parent_command to None."""
        from temporalio.contrib.langgraph._models import (
            ChannelWrite,
            NodeActivityOutput,
        )

        output = NodeActivityOutput(
            writes=[ChannelWrite(channel="value", value=1)],
        )

        assert output.parent_command is None


class TestMessageCoercion:
    """Tests for LangChain message coercion in state values."""

    def test_coerce_top_level_messages(self) -> None:
        """Top-level messages in state should be coerced to LangChain types."""
        from langchain_core.messages import AIMessage, HumanMessage

        from temporalio.contrib.langgraph._models import _coerce_state_values

        state = {
            "messages": [
                {"content": "hello", "type": "human"},
                {
                    "content": "",
                    "type": "ai",
                    "tool_calls": [
                        {
                            "name": "foo",
                            "args": {"x": 1},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                },
            ]
        }

        coerced = _coerce_state_values(state)

        # Messages should be converted to LangChain types
        assert isinstance(coerced["messages"][0], HumanMessage)
        assert isinstance(coerced["messages"][1], AIMessage)
        # AIMessage should have tool_calls attribute accessible
        assert hasattr(coerced["messages"][1], "tool_calls")
        assert coerced["messages"][1].tool_calls[0]["name"] == "foo"

    def test_coerce_nested_messages_in_tool_call_with_context(self) -> None:
        """Messages nested in tool_call_with_context.state should be coerced.

        When using Send API with create_react_agent or create_supervisor,
        the input state has structure:
        {
            "__type": "tool_call_with_context",
            "tool_call": {...},
            "state": {"messages": [...]}  # nested messages
        }

        The nested messages must also be coerced to LangChain types.
        """
        from langchain_core.messages import AIMessage, HumanMessage

        from temporalio.contrib.langgraph._models import _coerce_state_values

        state = {
            "__type": "tool_call_with_context",
            "tool_call": {
                "name": "calculator",
                "args": {"expression": "2 + 2"},
                "id": "call_123",
                "type": "tool_call",
            },
            "state": {
                "messages": [
                    {"content": "hello", "type": "human"},
                    {
                        "content": "",
                        "type": "ai",
                        "tool_calls": [
                            {
                                "name": "calculator",
                                "args": {"expression": "2 + 2"},
                                "id": "call_123",
                                "type": "tool_call",
                            }
                        ],
                    },
                ],
                "remaining_steps": 24,
            },
        }

        coerced = _coerce_state_values(state)

        # The nested state should also be coerced
        nested_state = coerced["state"]
        assert isinstance(nested_state, dict)
        assert "messages" in nested_state

        # Nested messages should be LangChain message objects
        assert isinstance(nested_state["messages"][0], HumanMessage)
        assert isinstance(nested_state["messages"][1], AIMessage)

        # AIMessage should have tool_calls as an attribute (not just dict key)
        ai_msg = nested_state["messages"][1]
        assert hasattr(
            ai_msg, "tool_calls"
        ), "AIMessage should have tool_calls attribute"
        assert ai_msg.tool_calls[0]["name"] == "calculator"

    def test_coerce_deeply_nested_messages(self) -> None:
        """Messages in arbitrarily nested dicts should be coerced."""
        from langchain_core.messages import HumanMessage

        from temporalio.contrib.langgraph._models import _coerce_state_values

        state = {
            "level1": {
                "level2": {
                    "messages": [
                        {"content": "deeply nested", "type": "human"},
                    ]
                }
            }
        }

        coerced = _coerce_state_values(state)

        nested_msg = coerced["level1"]["level2"]["messages"][0]
        assert isinstance(nested_msg, HumanMessage)
        assert nested_msg.content == "deeply nested"

    def test_node_activity_input_coerces_nested_state(self) -> None:
        """NodeActivityInput.__post_init__ should coerce nested messages.

        This simulates what happens when a tool node receives input via Send API
        from langgraph-supervisor or create_react_agent with subgraphs.
        """
        from langchain_core.messages import AIMessage

        from temporalio.contrib.langgraph._models import NodeActivityInput

        # Simulate serialized input that would come from Temporal
        # This is what tool_call_with_context looks like after JSON round-trip
        input_data = NodeActivityInput(
            node_name="tools",
            task_id="task_1",
            graph_id="test_graph",
            input_state={
                "__type": "tool_call_with_context",
                "tool_call": {
                    "name": "search",
                    "args": {"query": "test"},
                    "id": "call_abc",
                    "type": "tool_call",
                },
                "state": {
                    "messages": [
                        {
                            "content": "",
                            "type": "ai",
                            "tool_calls": [
                                {
                                    "name": "search",
                                    "args": {"query": "test"},
                                    "id": "call_abc",
                                    "type": "tool_call",
                                }
                            ],
                        }
                    ]
                },
            },
            config={},
            path=(),
            triggers=[],
        )

        # After __post_init__, nested messages should be coerced
        nested_state = input_data.input_state["state"]
        ai_msg = nested_state["messages"][0]

        assert isinstance(ai_msg, AIMessage), f"Expected AIMessage, got {type(ai_msg)}"
        assert hasattr(
            ai_msg, "tool_calls"
        ), "AIMessage should have tool_calls attribute"
