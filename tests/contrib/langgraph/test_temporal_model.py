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

    def test_bind_tools_raises_not_implemented(self) -> None:
        """bind_tools should raise NotImplementedError."""
        from temporalio.contrib.langgraph import temporal_model

        model = temporal_model(
            "gpt-4o-bind",
            start_to_close_timeout=timedelta(minutes=1),
        )

        with pytest.raises(NotImplementedError, match="Tool binding"):
            model.bind_tools([])
