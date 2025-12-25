"""Tests for LangGraph state serialization with Temporal.

These tests validate that LangGraph state can be serialized for Temporal
activities using Temporal's built-in data converters.

Technical Concern:
    Can LangGraph state be serialized for Temporal activities?

Answer: Yes, using Temporal's pydantic_data_converter for LangChain messages.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter


class TestLangChainMessagesArePydantic:
    """Verify LangChain messages are Pydantic models."""

    def test_human_message_is_pydantic(self) -> None:
        """HumanMessage should be a Pydantic BaseModel."""
        assert issubclass(HumanMessage, BaseModel)

    def test_ai_message_is_pydantic(self) -> None:
        """AIMessage should be a Pydantic BaseModel."""
        assert issubclass(AIMessage, BaseModel)

    def test_system_message_is_pydantic(self) -> None:
        """SystemMessage should be a Pydantic BaseModel."""
        assert issubclass(SystemMessage, BaseModel)

    def test_messages_have_model_dump(self) -> None:
        """Messages should have Pydantic v2 model_dump method."""
        msg = HumanMessage(content="test")
        assert hasattr(msg, "model_dump")
        dump = msg.model_dump()
        assert "content" in dump
        assert dump["content"] == "test"


class TestDefaultConverterWithBasicState:
    """Test Temporal's default converter with basic dict states."""

    def test_serialize_basic_dict(self) -> None:
        """Default converter should handle basic dict."""
        converter = DataConverter.default

        state: dict[str, Any] = {
            "count": 42,
            "name": "test",
        }

        payloads = converter.payload_converter.to_payloads([state])
        result = converter.payload_converter.from_payloads(payloads, [dict])

        assert result is not None
        assert result[0] == state

    def test_serialize_nested_dict(self) -> None:
        """Default converter should handle nested dicts."""
        converter = DataConverter.default

        state: dict[str, Any] = {
            "data": {"nested": {"deep": "value"}},
            "items": [1, 2, 3],
        }

        payloads = converter.payload_converter.to_payloads([state])
        result = converter.payload_converter.from_payloads(payloads, [dict])

        assert result is not None
        assert result[0] == state

    def test_serialize_list_of_strings(self) -> None:
        """Default converter should handle list of strings."""
        converter = DataConverter.default

        messages = ["hello", "world"]

        payloads = converter.payload_converter.to_payloads([messages])
        result = converter.payload_converter.from_payloads(payloads, [list])

        assert result is not None
        assert result[0] == messages


class TestPydanticConverterWithMessages:
    """Test Temporal's pydantic converter with LangChain messages."""

    def test_serialize_human_message(self) -> None:
        """Pydantic converter should serialize HumanMessage."""
        msg = HumanMessage(content="Hello, world!")

        payloads = pydantic_data_converter.payload_converter.to_payloads([msg])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [HumanMessage]
        )

        assert result is not None
        assert isinstance(result[0], HumanMessage)
        assert result[0].content == "Hello, world!"

    def test_serialize_ai_message(self) -> None:
        """Pydantic converter should serialize AIMessage."""
        msg = AIMessage(content="I am an AI assistant.")

        payloads = pydantic_data_converter.payload_converter.to_payloads([msg])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [AIMessage]
        )

        assert result is not None
        assert isinstance(result[0], AIMessage)
        assert result[0].content == "I am an AI assistant."

    def test_serialize_system_message(self) -> None:
        """Pydantic converter should serialize SystemMessage."""
        msg = SystemMessage(content="You are a helpful assistant.")

        payloads = pydantic_data_converter.payload_converter.to_payloads([msg])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [SystemMessage]
        )

        assert result is not None
        assert isinstance(result[0], SystemMessage)
        assert result[0].content == "You are a helpful assistant."

    def test_serialize_message_with_additional_kwargs(self) -> None:
        """Pydantic converter should preserve additional_kwargs."""
        msg = AIMessage(
            content="Response",
            additional_kwargs={"model": "gpt-4", "tokens": 100},
        )

        payloads = pydantic_data_converter.payload_converter.to_payloads([msg])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [AIMessage]
        )

        assert result is not None
        assert isinstance(result[0], AIMessage)
        assert result[0].content == "Response"
        assert result[0].additional_kwargs.get("model") == "gpt-4"
        assert result[0].additional_kwargs.get("tokens") == 100


class TestMultipleActivityParameters:
    """Test serializing multiple activity parameters.

    This simulates how activity parameters would be serialized.
    """

    def test_serialize_message_and_string(self) -> None:
        """Serialize a message and a string as separate parameters."""
        msg = HumanMessage(content="Hello")
        context = "greeting_context"

        payloads = pydantic_data_converter.payload_converter.to_payloads([msg, context])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [HumanMessage, str]
        )

        assert result is not None
        assert isinstance(result[0], HumanMessage)
        assert result[0].content == "Hello"
        assert result[1] == "greeting_context"

    def test_serialize_multiple_messages(self) -> None:
        """Serialize multiple messages as separate parameters."""
        human_msg = HumanMessage(content="What is 2+2?")
        ai_msg = AIMessage(content="4")

        payloads = pydantic_data_converter.payload_converter.to_payloads(
            [human_msg, ai_msg]
        )
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [HumanMessage, AIMessage]
        )

        assert result is not None
        assert isinstance(result[0], HumanMessage)
        assert isinstance(result[1], AIMessage)
        assert result[0].content == "What is 2+2?"
        assert result[1].content == "4"


class TestListOfMessages:
    """Test serializing lists of messages.

    LangGraph often uses lists of messages in state.
    """

    def test_serialize_list_of_messages_typed(self) -> None:
        """Serialize a list of messages with explicit typing."""
        messages = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there!"),
        ]

        # For lists, we need to serialize each message and reconstruct
        payloads_list = []
        for msg in messages:
            payloads = pydantic_data_converter.payload_converter.to_payloads([msg])
            payloads_list.append(payloads[0])

        # Deserialize back
        result = []
        for i, payload in enumerate(payloads_list):
            msg_type = type(messages[i])
            deserialized = pydantic_data_converter.payload_converter.from_payloads(
                [payload], [msg_type]
            )
            if deserialized:
                result.append(deserialized[0])

        assert len(result) == 2
        assert isinstance(result[0], HumanMessage)
        assert isinstance(result[1], AIMessage)
        assert result[0].content == "Hello"
        assert result[1].content == "Hi there!"


class TestRoundTrip:
    """Test round-trip serialization preserves data."""

    def test_round_trip_human_message(self) -> None:
        """Round-trip should preserve HumanMessage content."""
        original = HumanMessage(content="Test message content")

        payloads = pydantic_data_converter.payload_converter.to_payloads([original])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [HumanMessage]
        )

        assert result is not None
        assert result[0].content == original.content
        assert type(result[0]) == type(original)

    def test_round_trip_ai_message_with_metadata(self) -> None:
        """Round-trip should preserve AIMessage with metadata."""
        original = AIMessage(
            content="AI response",
            additional_kwargs={"finish_reason": "stop"},
        )

        payloads = pydantic_data_converter.payload_converter.to_payloads([original])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [AIMessage]
        )

        assert result is not None
        assert result[0].content == original.content
        assert result[0].additional_kwargs == original.additional_kwargs


# --- End-to-end activity test ---

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions


@activity.defn
async def process_message_activity(message: HumanMessage) -> AIMessage:
    """Activity that takes a HumanMessage and returns an AIMessage."""
    return AIMessage(
        content=f"Processed: {message.content}",
        additional_kwargs={"processed": True},
    )


@activity.defn
async def echo_messages_activity(messages: list[HumanMessage]) -> list[AIMessage]:
    """Activity that takes a list of messages and returns responses."""
    return [
        AIMessage(content=f"Echo: {msg.content}")
        for msg in messages
    ]


@workflow.defn
class MessageProcessingWorkflow:
    """Workflow that processes LangChain messages via activities."""

    @workflow.run
    async def run(self, input_message: HumanMessage) -> AIMessage:
        """Process a message through an activity."""
        return await workflow.execute_activity(
            process_message_activity,
            input_message,
            start_to_close_timeout=timedelta(seconds=10),
        )


@workflow.defn
class MultiMessageWorkflow:
    """Workflow that processes multiple messages."""

    @workflow.run
    async def run(self, messages: list[HumanMessage]) -> list[AIMessage]:
        """Process multiple messages through an activity."""
        return await workflow.execute_activity(
            echo_messages_activity,
            messages,
            start_to_close_timeout=timedelta(seconds=10),
        )


class TestEndToEndActivitySerialization:
    """End-to-end tests for activity serialization with real Temporal workflows."""

    @pytest.mark.asyncio
    async def test_activity_with_single_message(self) -> None:
        """Test workflow calling activity with HumanMessage input/AIMessage output."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = env.client

            # Configure sandbox to allow langchain imports
            # LangChain is used for type hints in workflow, so we need to passthrough
            sandbox_runner = SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "langchain_core",
                    "langchain_core.messages",
                    "langchain_core.messages.human",
                    "langchain_core.messages.ai",
                )
            )

            async with Worker(
                client,
                task_queue="test-queue",
                workflows=[MessageProcessingWorkflow],
                activities=[process_message_activity],
                workflow_runner=sandbox_runner,
            ):
                # Run workflow with HumanMessage input
                input_msg = HumanMessage(content="Hello from workflow!")

                result = await client.execute_workflow(
                    MessageProcessingWorkflow.run,
                    input_msg,
                    id="test-message-workflow",
                    task_queue="test-queue",
                )

                # Verify result is AIMessage with correct content
                assert isinstance(result, AIMessage)
                assert result.content == "Processed: Hello from workflow!"
                assert result.additional_kwargs.get("processed") is True

    @pytest.mark.asyncio
    async def test_activity_with_message_list(self) -> None:
        """Test workflow calling activity with list of messages."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = env.client

            # Configure sandbox to allow langchain imports
            sandbox_runner = SandboxedWorkflowRunner(
                restrictions=SandboxRestrictions.default.with_passthrough_modules(
                    "langchain_core",
                    "langchain_core.messages",
                    "langchain_core.messages.human",
                    "langchain_core.messages.ai",
                )
            )

            async with Worker(
                client,
                task_queue="test-queue",
                workflows=[MultiMessageWorkflow],
                activities=[echo_messages_activity],
                workflow_runner=sandbox_runner,
            ):
                # Run workflow with list of HumanMessages
                input_msgs = [
                    HumanMessage(content="First message"),
                    HumanMessage(content="Second message"),
                ]

                result = await client.execute_workflow(
                    MultiMessageWorkflow.run,
                    input_msgs,
                    id="test-multi-message-workflow",
                    task_queue="test-queue",
                )

                # Verify results
                assert len(result) == 2
                assert all(isinstance(msg, AIMessage) for msg in result)
                assert result[0].content == "Echo: First message"
                assert result[1].content == "Echo: Second message"


# --- NodeActivity Input/Output Validation ---


# --- Message Type Reconstruction Helpers ---

MESSAGE_TYPE_MAP: dict[str, type[BaseMessage]] = {
    "ai": AIMessage,
    "human": HumanMessage,
    "system": SystemMessage,
}


def reconstruct_message(data: dict[str, Any]) -> BaseMessage:
    """Reconstruct a LangChain message from its dict representation.

    When messages are serialized as part of Any-typed fields, they become dicts.
    This function reconstructs the proper message type using the 'type' field.
    """
    from langchain_core.messages import BaseMessage

    msg_type = data.get("type")
    msg_cls = MESSAGE_TYPE_MAP.get(msg_type)  # type: ignore[arg-type]
    if msg_cls:
        return msg_cls.model_validate(data)
    raise ValueError(f"Unknown message type: {msg_type}")


# --- Activity Input/Output Models ---


class ChannelWrite(BaseModel):
    """Single channel write with type preservation for LangChain messages.

    When values containing BaseMessage instances are serialized through
    pydantic_data_converter with Any type hints, they become plain dicts.
    This model preserves type information for reconstruction.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    channel: str
    """Channel name to write to."""

    value: Any
    """Value to write (may be dict after deserialization if was a message)."""

    value_type: str | None = None
    """Type hint for reconstruction: 'message', 'message_list', or None."""

    @classmethod
    def create(cls, channel: str, value: Any) -> "ChannelWrite":
        """Create a ChannelWrite, recording type info for messages."""
        from langchain_core.messages import BaseMessage

        value_type = None
        if isinstance(value, BaseMessage):
            value_type = "message"
        elif isinstance(value, list) and value and isinstance(value[0], BaseMessage):
            value_type = "message_list"
        return cls(channel=channel, value=value, value_type=value_type)

    def reconstruct_value(self) -> Any:
        """Reconstruct typed value from deserialized data."""
        if self.value_type == "message" and isinstance(self.value, dict):
            return reconstruct_message(self.value)
        elif self.value_type == "message_list" and isinstance(self.value, list):
            return [
                reconstruct_message(item) if isinstance(item, dict) else item
                for item in self.value
            ]
        return self.value

    def to_tuple(self) -> tuple[str, Any]:
        """Convert to (channel, value) tuple with reconstructed types."""
        return (self.channel, self.reconstruct_value())


class NodeActivityInput(BaseModel):
    """Pydantic model for NodeActivity input.

    This represents all data needed to execute a LangGraph node in a Temporal activity.
    Using a single Pydantic model ensures clean serialization.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    node_name: str
    """Name of the node to execute."""

    task_id: str
    """Unique task ID from PregelExecutableTask."""

    graph_builder_path: str
    """Module path to the graph builder function (e.g., 'myapp.agents.build_graph')."""

    input_state: dict[str, Any]
    """Input state to pass to the node. May contain serialized messages."""

    config: dict[str, Any]
    """Filtered RunnableConfig (internal keys removed)."""

    path: tuple[str | int, ...]
    """Graph hierarchy path."""

    triggers: list[str]
    """Channels that triggered this task."""


class NodeActivityOutput(BaseModel):
    """Pydantic model for NodeActivity output.

    Contains the writes produced by node execution. Uses ChannelWrite
    to preserve LangChain message types through serialization.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    writes: list[ChannelWrite]
    """List of channel writes produced by the node."""

    def to_write_tuples(self) -> list[tuple[str, Any]]:
        """Convert to list of (channel, value) tuples with proper types."""
        return [w.to_tuple() for w in self.writes]


class TestNodeActivityInputSerialization:
    """Test serialization of the full NodeActivity input structure."""

    def test_serialize_node_activity_input_basic(self) -> None:
        """Serialize NodeActivityInput with basic state."""
        input_data = NodeActivityInput(
            node_name="process_node",
            task_id="task-123-abc",
            graph_builder_path="myapp.agents.build_graph",
            input_state={"count": 42, "name": "test"},
            config={"tags": ["test"], "metadata": {"source": "unit_test"}},
            path=("process_node",),
            triggers=["start"],
        )

        payloads = pydantic_data_converter.payload_converter.to_payloads([input_data])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [NodeActivityInput]
        )

        assert result is not None
        assert isinstance(result[0], NodeActivityInput)
        assert result[0].node_name == "process_node"
        assert result[0].task_id == "task-123-abc"
        assert result[0].graph_builder_path == "myapp.agents.build_graph"
        assert result[0].input_state == {"count": 42, "name": "test"}
        assert result[0].config == {"tags": ["test"], "metadata": {"source": "unit_test"}}
        assert result[0].path == ("process_node",)
        assert result[0].triggers == ["start"]

    def test_serialize_node_activity_input_with_nested_path(self) -> None:
        """Serialize NodeActivityInput with nested subgraph path."""
        input_data = NodeActivityInput(
            node_name="inner_node",
            task_id="task-456-def",
            graph_builder_path="myapp.agents.build_graph",
            input_state={"messages": ["hello", "world"]},
            config={},
            path=("outer_graph", 0, "inner_node"),
            triggers=["branch:left"],
        )

        payloads = pydantic_data_converter.payload_converter.to_payloads([input_data])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [NodeActivityInput]
        )

        assert result is not None
        assert result[0].path == ("outer_graph", 0, "inner_node")
        assert result[0].triggers == ["branch:left"]


class TestNodeActivityOutputSerialization:
    """Test serialization of NodeActivity output structure."""

    def test_serialize_node_activity_output_basic(self) -> None:
        """Serialize NodeActivityOutput with basic writes."""
        output_data = NodeActivityOutput(
            writes=[
                ChannelWrite.create("messages", {"content": "processed"}),
                ChannelWrite.create("count", 43),
            ]
        )

        payloads = pydantic_data_converter.payload_converter.to_payloads([output_data])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [NodeActivityOutput]
        )

        assert result is not None
        assert isinstance(result[0], NodeActivityOutput)
        assert len(result[0].writes) == 2
        # Verify via to_write_tuples which handles reconstruction
        tuples = result[0].to_write_tuples()
        assert tuples[0] == ("messages", {"content": "processed"})
        assert tuples[1] == ("count", 43)

    def test_serialize_node_activity_output_empty(self) -> None:
        """Serialize NodeActivityOutput with no writes."""
        output_data = NodeActivityOutput(writes=[])

        payloads = pydantic_data_converter.payload_converter.to_payloads([output_data])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [NodeActivityOutput]
        )

        assert result is not None
        assert result[0].writes == []

    def test_serialize_node_activity_output_with_messages(self) -> None:
        """Serialize NodeActivityOutput with LangChain messages - critical test."""
        output_data = NodeActivityOutput(
            writes=[
                ChannelWrite.create("messages", AIMessage(content="Hello from AI")),
                ChannelWrite.create("count", 42),
                ChannelWrite.create(
                    "history",
                    [HumanMessage(content="Hi"), AIMessage(content="Hello!")],
                ),
            ]
        )

        payloads = pydantic_data_converter.payload_converter.to_payloads([output_data])
        result = pydantic_data_converter.payload_converter.from_payloads(
            payloads, [NodeActivityOutput]
        )

        assert result is not None
        tuples = result[0].to_write_tuples()

        # Verify messages are properly reconstructed
        channel, value = tuples[0]
        assert channel == "messages"
        assert isinstance(value, AIMessage)
        assert value.content == "Hello from AI"

        # Verify primitive preserved
        assert tuples[1] == ("count", 42)

        # Verify message list reconstructed with correct types
        channel, value = tuples[2]
        assert channel == "history"
        assert isinstance(value, list)
        assert isinstance(value[0], HumanMessage)
        assert isinstance(value[1], AIMessage)
        assert value[0].content == "Hi"
        assert value[1].content == "Hello!"


# --- End-to-end NodeActivity test ---


@activity.defn
async def execute_node_activity(input_data: NodeActivityInput) -> NodeActivityOutput:
    """Activity that simulates executing a LangGraph node.

    In real implementation, this would:
    1. Import the graph builder
    2. Rebuild the graph
    3. Get the node by name
    4. Execute the node with input_state
    5. Return the writes

    For this test, we simulate the execution.
    """
    # Simulate node execution based on node_name
    if input_data.node_name == "increment":
        count = input_data.input_state.get("count", 0)
        return NodeActivityOutput(
            writes=[ChannelWrite.create("count", count + 1)]
        )
    elif input_data.node_name == "process_messages":
        messages = input_data.input_state.get("messages", [])
        processed = [f"processed: {m}" for m in messages]
        return NodeActivityOutput(
            writes=[ChannelWrite.create("messages", processed)]
        )
    else:
        return NodeActivityOutput(writes=[])


@workflow.defn(sandboxed=False)
class NodeActivityWorkflow:
    """Workflow that executes a node via activity.

    Note: sandboxed=False because NodeActivityInput/Output are defined in test module.
    In production, these would be in a proper module with passthrough configured.
    """

    @workflow.run
    async def run(self, input_data: NodeActivityInput) -> NodeActivityOutput:
        """Execute a node through an activity."""
        return await workflow.execute_activity(
            execute_node_activity,
            input_data,
            start_to_close_timeout=timedelta(seconds=10),
        )


class TestEndToEndNodeActivitySerialization:
    """End-to-end tests for NodeActivity input/output serialization.

    These tests validate that NodeActivityInput and NodeActivityOutput
    can be serialized through a real Temporal workflow/activity round-trip.
    """

    @pytest.mark.asyncio
    async def test_node_activity_increment(self) -> None:
        """Test full round-trip with increment node simulation."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[NodeActivityWorkflow],
                activities=[execute_node_activity],
            ):
                input_data = NodeActivityInput(
                    node_name="increment",
                    task_id="task-001",
                    graph_builder_path="myapp.agents.build_graph",
                    input_state={"count": 10},
                    config={"tags": ["test"]},
                    path=("increment",),
                    triggers=["start"],
                )

                result = await env.client.execute_workflow(
                    NodeActivityWorkflow.run,
                    input_data,
                    id="test-node-activity-workflow",
                    task_queue="test-queue",
                )

                assert isinstance(result, NodeActivityOutput)
                assert len(result.writes) == 1
                write_tuples = result.to_write_tuples()
                assert write_tuples[0] == ("count", 11)

    @pytest.mark.asyncio
    async def test_node_activity_process_messages(self) -> None:
        """Test full round-trip with message processing node simulation."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[NodeActivityWorkflow],
                activities=[execute_node_activity],
            ):
                input_data = NodeActivityInput(
                    node_name="process_messages",
                    task_id="task-002",
                    graph_builder_path="myapp.agents.build_graph",
                    input_state={"messages": ["hello", "world"]},
                    config={},
                    path=("outer", "process_messages"),
                    triggers=["branch:main"],
                )

                result = await env.client.execute_workflow(
                    NodeActivityWorkflow.run,
                    input_data,
                    id="test-node-activity-workflow-messages",
                    task_queue="test-queue",
                )

                assert isinstance(result, NodeActivityOutput)
                assert len(result.writes) == 1
                write_tuples = result.to_write_tuples()
                channel, value = write_tuples[0]
                assert channel == "messages"
                assert value == ["processed: hello", "processed: world"]

    @pytest.mark.asyncio
    async def test_node_activity_with_complex_config(self) -> None:
        """Test with complex filtered config."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue="test-queue",
                workflows=[NodeActivityWorkflow],
                activities=[execute_node_activity],
            ):
                input_data = NodeActivityInput(
                    node_name="increment",
                    task_id="task-003",
                    graph_builder_path="myapp.complex.nested.module.build_graph",
                    input_state={
                        "count": 100,
                        "metadata": {"source": "api", "user_id": "user-123"},
                        "nested": {"deep": {"value": [1, 2, 3]}},
                    },
                    config={
                        "tags": ["production", "high-priority"],
                        "metadata": {"run_id": "run-456", "version": "1.0"},
                        "configurable": {
                            "user_setting": "custom_value",
                            "feature_flags": {"flag_a": True, "flag_b": False},
                        },
                    },
                    path=("main", 0, "sub", 1, "increment"),
                    triggers=["channel:data", "channel:trigger"],
                )

                result = await env.client.execute_workflow(
                    NodeActivityWorkflow.run,
                    input_data,
                    id="test-node-activity-complex",
                    task_queue="test-queue",
                )

                assert isinstance(result, NodeActivityOutput)
                write_tuples = result.to_write_tuples()
                assert write_tuples[0] == ("count", 101)