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


