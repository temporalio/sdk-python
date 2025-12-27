"""Unit tests for temporal_model() wrapper.

Tests for wrapping LangChain chat models with Temporal activity execution.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from temporalio.common import RetryPolicy


class TestTemporalModel:
    """Tests for the temporal_model() wrapper."""

    def test_wrap_model_with_string_name(self) -> None:
        """Should create wrapper from model name string."""
        from temporalio.contrib.langgraph import temporal_model

        model = temporal_model(
            "gpt-4o",
            start_to_close_timeout=timedelta(minutes=2),
        )

        assert model is not None
        assert model._llm_type == "temporal-chat-model"

    def test_wrap_model_with_instance(self) -> None:
        """Should wrap a model instance."""
        from temporalio.contrib.langgraph import temporal_model
        from temporalio.contrib.langgraph._model_registry import get_model

        # Create a mock model
        mock_base_model = MagicMock()
        mock_base_model.model_name = "mock-model"
        mock_base_model._agenerate = AsyncMock()

        model = temporal_model(
            mock_base_model,
            start_to_close_timeout=timedelta(minutes=2),
        )

        assert model is not None
        # Model instance should be registered
        assert get_model("mock-model") is mock_base_model

    def test_wrap_model_with_all_options(self) -> None:
        """Should accept all activity options."""
        from temporalio.contrib.langgraph import temporal_model

        # Should not raise
        model = temporal_model(
            "test-model",
            start_to_close_timeout=timedelta(minutes=5),
            schedule_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=30),
            task_queue="llm-workers",
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        assert model is not None

    def test_wrapped_model_raises_outside_workflow_with_string(self) -> None:
        """When not in workflow with string model, should raise."""
        from langchain_core.messages import HumanMessage

        from temporalio.contrib.langgraph import temporal_model

        model = temporal_model(
            "gpt-4o-not-registered",
            start_to_close_timeout=timedelta(minutes=1),
        )

        async def run_test():
            with patch("temporalio.workflow.in_workflow", return_value=False):
                with pytest.raises(RuntimeError, match="Cannot invoke"):
                    await model._agenerate([HumanMessage(content="Hello")])

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_wrapped_model_runs_directly_outside_workflow_with_instance(self) -> None:
        """When not in workflow with model instance, should execute directly."""
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        from temporalio.contrib.langgraph import temporal_model

        # Create a mock model that tracks whether _agenerate was called
        call_tracker: dict[str, bool] = {"called": False}

        async def mock_agenerate(messages: Any, **kwargs: Any) -> ChatResult:
            call_tracker["called"] = True
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(content="Hello from model"),
                    )
                ]
            )

        mock_base_model = MagicMock()
        mock_base_model.model_name = "direct-mock-model"
        mock_base_model._agenerate = mock_agenerate

        model = temporal_model(
            mock_base_model,
            start_to_close_timeout=timedelta(minutes=1),
        )

        async def run_test():
            # Patch in the module where it's used
            with patch(
                "temporalio.contrib.langgraph._temporal_model.workflow.in_workflow",
                return_value=False,
            ):
                result = await model._agenerate([HumanMessage(content="Hello")])
                # Verify result content
                assert result.generations[0].message.content == "Hello from model"
                # Verify the underlying model was called
                assert call_tracker[
                    "called"
                ], "Expected underlying model._agenerate to be called"

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_wrapped_model_executes_as_activity_in_workflow(self) -> None:
        """When in workflow, wrapped model should execute as activity."""
        from langchain_core.messages import HumanMessage

        from temporalio.contrib.langgraph import temporal_model
        from temporalio.contrib.langgraph._models import ChatModelActivityOutput

        model = temporal_model(
            "gpt-4o-activity",
            start_to_close_timeout=timedelta(minutes=2),
        )

        # Mock activity result
        mock_result = ChatModelActivityOutput(
            generations=[
                {
                    "message": {"content": "Activity response", "type": "ai"},
                    "generation_info": None,
                }
            ],
            llm_output=None,
        )

        async def run_test():
            with patch("temporalio.workflow.in_workflow", return_value=True):
                with patch("temporalio.workflow.unsafe.imports_passed_through"):
                    with patch(
                        "temporalio.workflow.execute_activity",
                        new_callable=AsyncMock,
                        return_value=mock_result,
                    ) as mock_execute:
                        result = await model._agenerate([HumanMessage(content="Hello")])

                        # Verify activity was called
                        mock_execute.assert_called_once()
                        call_args = mock_execute.call_args
                        assert call_args[1]["start_to_close_timeout"] == timedelta(
                            minutes=2
                        )

                        # Result should be reconstructed
                        assert len(result.generations) == 1
                        assert (
                            result.generations[0].message.content == "Activity response"
                        )

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_bind_tools_with_dict_schemas(self) -> None:
        """bind_tools should accept dict tool schemas."""
        from temporalio.contrib.langgraph import temporal_model

        model = temporal_model(
            "gpt-4o-bind",
            start_to_close_timeout=timedelta(minutes=1),
        )

        # Tool schema as dict
        tool_schema = {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }

        bound_model: Any = model.bind_tools([tool_schema])

        # Should return a new model instance
        assert bound_model is not model
        assert bound_model._llm_type == "temporal-chat-model"
        # Tools should be stored
        assert bound_model._temporal_bound_tools == [tool_schema]

    def test_bind_tools_with_langchain_tool(self) -> None:
        """bind_tools should convert LangChain tools to schemas."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_model

        @tool
        def calculator(expression: str) -> str:
            """Calculate a math expression."""
            return str(eval(expression))

        model = temporal_model(
            "gpt-4o-bind-tool",
            start_to_close_timeout=timedelta(minutes=1),
        )

        bound_model: Any = model.bind_tools([calculator])

        assert bound_model is not model
        assert len(bound_model._temporal_bound_tools) == 1
        # Should be converted to OpenAI format
        tool_schema = bound_model._temporal_bound_tools[0]
        assert tool_schema["type"] == "function"
        assert tool_schema["function"]["name"] == "calculator"

    def test_bind_tools_with_tool_choice(self) -> None:
        """bind_tools should pass through tool_choice."""
        from temporalio.contrib.langgraph import temporal_model

        model = temporal_model(
            "gpt-4o-bind-choice",
            start_to_close_timeout=timedelta(minutes=1),
        )

        tool_schema = {
            "type": "function",
            "function": {"name": "test_tool", "parameters": {}},
        }

        bound_model: Any = model.bind_tools([tool_schema], tool_choice="auto")

        assert bound_model._temporal_tool_choice == "auto"

    def test_bind_tools_preserves_activity_options(self) -> None:
        """bind_tools should preserve activity options."""
        from temporalio.contrib.langgraph import temporal_model

        model = temporal_model(
            "gpt-4o-bind-options",
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=30),
            task_queue="custom-queue",
        )

        bound_model: Any = model.bind_tools([])

        assert (
            bound_model._temporal_activity_options["start_to_close_timeout"]
            == timedelta(minutes=5)
        )
        assert bound_model._temporal_activity_options["heartbeat_timeout"] == timedelta(
            seconds=30
        )
        assert bound_model._temporal_activity_options["task_queue"] == "custom-queue"

    def test_bind_tools_passes_tools_to_activity(self) -> None:
        """When in workflow, bound tools should be passed to activity."""
        from langchain_core.messages import HumanMessage

        from temporalio.contrib.langgraph import temporal_model
        from temporalio.contrib.langgraph._models import ChatModelActivityOutput

        model = temporal_model(
            "gpt-4o-activity-tools",
            start_to_close_timeout=timedelta(minutes=2),
        )

        tool_schema = {
            "type": "function",
            "function": {"name": "test_tool", "parameters": {}},
        }

        bound_model = model.bind_tools([tool_schema], tool_choice="required")

        mock_result = ChatModelActivityOutput(
            generations=[
                {
                    "message": {"content": "", "type": "ai", "tool_calls": []},
                    "generation_info": None,
                }
            ],
            llm_output=None,
        )

        async def run_test():
            with patch("temporalio.workflow.in_workflow", return_value=True):
                with patch("temporalio.workflow.unsafe.imports_passed_through"):
                    with patch(
                        "temporalio.workflow.execute_activity",
                        new_callable=AsyncMock,
                        return_value=mock_result,
                    ) as mock_execute:
                        await bound_model._agenerate([HumanMessage(content="Hello")])

                        # Verify activity was called with tools
                        mock_execute.assert_called_once()
                        call_args = mock_execute.call_args
                        activity_input = call_args[0][1]  # Second positional arg

                        assert activity_input.tools == [tool_schema]
                        assert activity_input.tool_choice == "required"

        asyncio.get_event_loop().run_until_complete(run_test())
