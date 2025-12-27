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
