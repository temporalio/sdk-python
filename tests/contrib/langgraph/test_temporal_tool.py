"""Unit tests for temporal_tool() wrapper.

Tests for wrapping LangChain tools with Temporal activity execution.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from temporalio.common import RetryPolicy


class TestTemporalTool:
    """Tests for the temporal_tool() wrapper."""

    def test_wrap_tool_preserves_metadata(self) -> None:
        """Wrapped tool should preserve name, description, args_schema."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_tool

        @tool
        def search_web(query: str) -> str:
            """Search the web for information."""
            return f"Results for: {query}"

        wrapped = temporal_tool(
            search_web,
            start_to_close_timeout=timedelta(minutes=2),
        )

        assert wrapped.name == "search_web"
        assert wrapped.description == "Search the web for information."

    def test_wrap_tool_with_all_options(self) -> None:
        """Should accept all activity options."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_tool

        @tool
        def my_tool(x: str) -> str:
            """Test tool."""
            return x

        # Should not raise
        wrapped = temporal_tool(
            my_tool,
            start_to_close_timeout=timedelta(minutes=5),
            schedule_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=30),
            task_queue="custom-queue",
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        assert wrapped is not None
        assert wrapped.name == "my_tool"

    def test_wrap_tool_registers_in_registry(self) -> None:
        """temporal_tool should register the tool in the global registry."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_tool
        from temporalio.contrib.langgraph._tool_registry import get_tool

        @tool
        def registered_tool(x: str) -> str:
            """A registered tool."""
            return x

        temporal_tool(registered_tool, start_to_close_timeout=timedelta(minutes=1))

        # Original tool should be in registry
        assert get_tool("registered_tool") is registered_tool

    def test_wrapped_tool_runs_directly_outside_workflow(self) -> None:
        """When not in workflow, wrapped tool should execute directly."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_tool

        @tool
        def direct_tool(query: str) -> str:
            """A tool that runs directly."""
            return f"Direct: {query}"

        wrapped = temporal_tool(
            direct_tool,
            start_to_close_timeout=timedelta(minutes=1),
        )

        # Mock workflow.in_workflow to return False
        with patch("temporalio.workflow.in_workflow", return_value=False):
            result = asyncio.get_event_loop().run_until_complete(
                wrapped.ainvoke({"query": "test"})
            )
            assert result == "Direct: test"

    def test_wrapped_tool_executes_as_activity_in_workflow(self) -> None:
        """When in workflow, wrapped tool should execute as activity."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_tool
        from temporalio.contrib.langgraph._models import ToolActivityOutput

        @tool
        def activity_tool(query: str) -> str:
            """A tool that runs as activity."""
            return f"Activity: {query}"

        wrapped = temporal_tool(
            activity_tool,
            start_to_close_timeout=timedelta(minutes=1),
        )

        # Mock workflow context
        mock_result = ToolActivityOutput(output="Activity result")

        async def run_test():
            with patch("temporalio.workflow.in_workflow", return_value=True):
                with patch("temporalio.workflow.unsafe.imports_passed_through"):
                    with patch(
                        "temporalio.workflow.execute_activity",
                        new_callable=AsyncMock,
                        return_value=mock_result,
                    ) as mock_execute:
                        result = await wrapped._arun(query="test")

                        # Verify activity was called
                        mock_execute.assert_called_once()
                        call_args = mock_execute.call_args
                        assert call_args[1]["start_to_close_timeout"] == timedelta(
                            minutes=1
                        )

                        assert result == "Activity result"

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_wrap_structured_tool(self) -> None:
        """Should wrap StructuredTool instances."""
        from langchain_core.tools import StructuredTool

        from temporalio.contrib.langgraph import temporal_tool

        def calculator(expression: str) -> float:
            """Calculate a math expression."""
            return eval(expression)

        structured = StructuredTool.from_function(
            calculator,
            name="calculator",
            description="Calculate math expressions",
        )

        wrapped = temporal_tool(
            structured,
            start_to_close_timeout=timedelta(minutes=1),
        )

        assert wrapped.name == "calculator"
        assert "Calculate" in wrapped.description

    def test_wrap_non_tool_raises(self) -> None:
        """Should raise TypeError for non-tool objects."""
        from temporalio.contrib.langgraph import temporal_tool

        with pytest.raises(TypeError, match="Expected BaseTool"):
            temporal_tool(
                "not a tool",  # type: ignore
                start_to_close_timeout=timedelta(minutes=1),
            )
