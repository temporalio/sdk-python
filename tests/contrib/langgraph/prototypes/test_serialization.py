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
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

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
