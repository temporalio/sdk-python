"""Unit tests for TemporalLangGraphRunner.

Tests for runner initialization, configuration, and basic behavior.
These tests mock the workflow context and don't require a running Temporal server.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph
from temporalio.common import RetryPolicy

from temporalio.contrib.langgraph import node_activity_options


class TestTemporalLangGraphRunner:
    """Tests for the Temporal runner."""

    def test_runner_rejects_step_timeout(self) -> None:
        """Runner should reject graphs with step_timeout."""
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        # Create a mock Pregel with step_timeout
        mock_pregel = MagicMock()
        mock_pregel.step_timeout = 30  # Non-None value

        with pytest.raises(ValueError, match="step_timeout"):
            TemporalLangGraphRunner(
                mock_pregel,
                graph_id="test",
            )

    def test_runner_accepts_no_step_timeout(self) -> None:
        """Runner should accept graphs without step_timeout."""
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {}

        runner = TemporalLangGraphRunner(
            mock_pregel,
            graph_id="test",
        )

        assert runner.graph_id == "test"
        assert runner.default_activity_options == {}

    def test_runner_invoke_raises(self) -> None:
        """Synchronous invoke should raise NotImplementedError."""
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test")

        with pytest.raises(NotImplementedError, match="ainvoke"):
            runner.invoke({})

    def test_filter_config(self) -> None:
        """Runner should filter internal config keys."""
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test")

        config = {
            "user_key": "value",
            "__pregel_internal": "hidden",
            "__lg_internal": "also_hidden",
            "configurable": {
                "thread_id": "123",
                "__pregel_key": "hidden",
            },
        }

        filtered = runner._filter_config(config)

        assert "user_key" in filtered
        assert "__pregel_internal" not in filtered
        assert "__lg_internal" not in filtered
        assert "configurable" in filtered
        assert "thread_id" in filtered["configurable"]
        assert "__pregel_key" not in filtered["configurable"]


class TestBuildActivitySummary:
    """Tests for the _build_activity_summary function."""

    def test_returns_node_name_for_non_tools_node(self) -> None:
        """Non-tools nodes should return just the node name."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        result = _build_activity_summary("agent", {"messages": []})
        assert result == "agent"

        result = _build_activity_summary("process", {"data": "value"})
        assert result == "process"

    def test_returns_node_name_when_no_tool_calls(self) -> None:
        """Tools node without tool calls should return node name."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        result = _build_activity_summary("tools", {"messages": []})
        assert result == "tools"

        result = _build_activity_summary("tools", {"messages": [{"content": "hello"}]})
        assert result == "tools"

    def test_extracts_tool_calls_from_dict_message(self) -> None:
        """Should extract tool calls from dict-style messages."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {
                    "content": "",
                    "tool_calls": [
                        {"name": "get_weather", "args": {"city": "Tokyo"}},
                    ],
                }
            ]
        }

        result = _build_activity_summary("tools", input_state)
        assert result == "get_weather({'city': 'Tokyo'})"

    def test_extracts_tool_calls_from_langchain_message(self) -> None:
        """Should extract tool calls from LangChain AIMessage objects."""
        from langchain_core.messages import AIMessage

        from temporalio.contrib.langgraph._runner import _build_activity_summary

        msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "calculate", "args": {"expression": "2 + 2"}, "id": "call_1"},
            ],
        )
        input_state = {"messages": [msg]}

        result = _build_activity_summary("tools", input_state)
        assert result == "calculate({'expression': '2 + 2'})"

    def test_handles_multiple_tool_calls(self) -> None:
        """Should handle multiple tool calls in a single message."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {
                    "tool_calls": [
                        {"name": "tool1", "args": {"a": 1}},
                        {"name": "tool2", "args": {"b": 2}},
                    ],
                }
            ]
        }

        result = _build_activity_summary("tools", input_state)
        assert result == "tool1({'a': 1}), tool2({'b': 2})"

    def test_truncates_long_summaries(self) -> None:
        """Should truncate summaries longer than max_length."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {
                    "tool_calls": [
                        {"name": "search", "args": {"query": "a" * 200}},
                    ],
                }
            ]
        }

        result = _build_activity_summary("tools", input_state, max_length=50)
        assert len(result) == 50
        assert result.endswith("...")

    def test_handles_non_dict_input_state(self) -> None:
        """Should handle non-dict input states gracefully."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        result = _build_activity_summary("tools", "not a dict")
        assert result == "tools"

        result = _build_activity_summary("tools", None)
        assert result == "tools"

    def test_extracts_tool_calls_from_send_packet(self) -> None:
        """Should extract tool calls from Send packet structure (tool_call_with_context)."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        # This is the structure used by create_react_agent when executing tools via Send
        input_state = {
            "__type": "tool_call_with_context",
            "tool_call": {
                "name": "calculator",
                "args": {"expression": "2 + 2"},
                "id": "call_123",
                "type": "tool_call",
            },
            "state": {
                "messages": [],
                "remaining_steps": 24,
            },
        }

        result = _build_activity_summary("tools", input_state)
        assert result == "calculator({'expression': '2 + 2'})"

    def test_uses_node_metadata_description(self) -> None:
        """Should use node metadata description when available."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        node_metadata = {"description": "Process user input and generate response"}
        result = _build_activity_summary("agent", {"messages": []}, node_metadata)
        assert result == "Process user input and generate response"

    def test_truncates_long_description(self) -> None:
        """Should truncate description longer than max_length."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        long_description = "A" * 200
        node_metadata = {"description": long_description}
        result = _build_activity_summary("node", {}, node_metadata, max_length=50)
        assert len(result) == 50
        assert result.endswith("...")

    def test_tool_calls_take_precedence_over_description(self) -> None:
        """For tools node with tool calls, tool info should take precedence over description."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "__type": "tool_call_with_context",
            "tool_call": {
                "name": "get_weather",
                "args": {"city": "NYC"},
                "id": "call_123",
            },
        }
        node_metadata = {"description": "Execute tool calls"}
        result = _build_activity_summary("tools", input_state, node_metadata)
        assert result == "get_weather({'city': 'NYC'})"

    def test_description_used_when_no_tool_calls(self) -> None:
        """For tools node without tool calls, should fall back to description."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        node_metadata = {"description": "Execute tool calls"}
        result = _build_activity_summary("tools", {"messages": []}, node_metadata)
        assert result == "Execute tool calls"

    def test_ignores_non_string_description(self) -> None:
        """Should ignore description if not a string."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        node_metadata = {"description": 123}  # Not a string
        result = _build_activity_summary("agent", {}, node_metadata)
        assert result == "agent"

    def test_ignores_empty_description(self) -> None:
        """Should ignore empty description."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        node_metadata = {"description": ""}
        result = _build_activity_summary("agent", {}, node_metadata)
        assert result == "agent"


class TestCompileFunction:
    """Tests for the compile() public API."""

    def test_compile_returns_runner(self) -> None:
        """compile() should return a TemporalLangGraphRunner."""
        from temporalio.contrib.langgraph import (
            LangGraphPlugin,
            TemporalLangGraphRunner,
            compile,
        )

        class State(TypedDict, total=False):
            value: int

        def build_compile_test():
            graph = StateGraph(State)
            graph.add_node("node", lambda state: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        # Register via plugin
        LangGraphPlugin(graphs={"compile_test": build_compile_test})

        # compile() should work
        runner = compile("compile_test")
        assert isinstance(runner, TemporalLangGraphRunner)
        assert runner.graph_id == "compile_test"

    def test_compile_nonexistent_raises(self) -> None:
        """compile() should raise ApplicationError for unregistered graph."""
        from temporalio.contrib.langgraph import GRAPH_NOT_FOUND_ERROR, compile
        from temporalio.exceptions import ApplicationError

        with pytest.raises(ApplicationError) as exc_info:
            compile("nonexistent_graph")
        assert exc_info.value.type == GRAPH_NOT_FOUND_ERROR

    def test_compile_with_options(self) -> None:
        """compile() should pass options to runner."""
        from temporalio.contrib.langgraph import LangGraphPlugin, compile

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node("node", lambda state: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        LangGraphPlugin(graphs={"options_test": build})

        runner = compile(
            "options_test",
            default_activity_options=node_activity_options(
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=5),
                task_queue="custom-queue",
            ),
        )

        assert runner.default_activity_options["start_to_close_timeout"] == timedelta(
            minutes=10
        )
        assert runner.default_activity_options["retry_policy"].maximum_attempts == 5
        assert runner.default_activity_options["task_queue"] == "custom-queue"
