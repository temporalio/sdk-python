"""Unit tests for LangGraphPlugin.

Tests for plugin initialization, activity registration, and worker integration.
"""

from __future__ import annotations

from datetime import timedelta

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict


class _TestState(TypedDict, total=False):
    """Test state for unit tests."""

    value: int


def _make_test_graph():
    """Create a simple test graph."""
    graph = StateGraph(_TestState)
    graph.add_node("node", lambda state: {"value": 1})
    graph.add_edge(START, "node")
    graph.add_edge("node", END)
    return graph.compile()


class TestLangGraphPlugin:
    """Tests for LangGraphPlugin initialization and configuration."""

    def test_plugin_initialization(self) -> None:
        """Plugin should initialize with graph factories."""
        from temporalio.contrib.langgraph import LangGraphPlugin, activity_options

        plugin = LangGraphPlugin(
            graphs={"my_graph": _make_test_graph},
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(seconds=30),
            ),
        )

        # Graph should be registered
        assert "my_graph" in plugin._graphs
        assert plugin._default_activity_options is not None

    def test_plugin_with_multiple_graphs(self) -> None:
        """Plugin should support multiple graphs."""
        from temporalio.contrib.langgraph import LangGraphPlugin, activity_options

        plugin = LangGraphPlugin(
            graphs={
                "graph_a": _make_test_graph,
                "graph_b": _make_test_graph,
            },
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(seconds=60),
            ),
        )

        assert len(plugin._graphs) == 2
        assert "graph_a" in plugin._graphs
        assert "graph_b" in plugin._graphs

    def test_plugin_activity_options(self) -> None:
        """Plugin should support custom activity options."""
        from temporalio.contrib.langgraph import LangGraphPlugin, activity_options

        default_opts = activity_options(start_to_close_timeout=timedelta(seconds=120))
        # Global option (string key)
        slow_node_opts = activity_options(start_to_close_timeout=timedelta(seconds=300))
        # Scoped option (tuple key)
        scoped_key: tuple[str, str] = ("my_graph", "scoped_node")
        scoped_opts = activity_options(start_to_close_timeout=timedelta(seconds=600))

        plugin = LangGraphPlugin(
            graphs={"my_graph": _make_test_graph},
            default_activity_options=default_opts,
            activity_options={
                "slow_node": slow_node_opts,
                scoped_key: scoped_opts,
            },
        )

        assert plugin._default_activity_options == default_opts
        assert "slow_node" in plugin._activity_options
        assert scoped_key in plugin._activity_options

    def test_plugin_get_graph_ids(self) -> None:
        """Plugin should return registered graph IDs."""
        from temporalio.contrib.langgraph import LangGraphPlugin, activity_options

        plugin = LangGraphPlugin(
            graphs={
                "graph_a": _make_test_graph,
                "graph_b": _make_test_graph,
            },
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(seconds=30),
            ),
        )

        graph_ids = plugin.get_graph_ids()
        assert "graph_a" in graph_ids
        assert "graph_b" in graph_ids

    def test_plugin_is_registered(self) -> None:
        """Plugin should check if graph is registered."""
        from temporalio.contrib.langgraph import LangGraphPlugin, activity_options

        plugin = LangGraphPlugin(
            graphs={"my_graph": _make_test_graph},
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(seconds=30),
            ),
        )

        assert plugin.is_registered("my_graph")
        assert not plugin.is_registered("nonexistent")


class TestPluginWorkerIntegration:
    """Tests for plugin-worker integration (without running actual worker)."""

    def test_plugin_creates_graph_registry_entries(self) -> None:
        """Plugin should register graphs in the global registry on init."""
        from temporalio.contrib.langgraph import LangGraphPlugin, activity_options
        from temporalio.contrib.langgraph._graph_registry import get_global_registry

        class State(TypedDict, total=False):
            value: int

        def build_graph():
            graph = StateGraph(State)
            graph.add_node("node", lambda state: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        registry = get_global_registry()

        # Before plugin - registry should be clear due to conftest.py fixture
        assert not registry.is_registered("integration_test_graph")

        # Create plugin - this registers graphs automatically
        LangGraphPlugin(
            graphs={"integration_test_graph": build_graph},
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(seconds=30),
            ),
        )

        # After plugin init, graph should be registered
        assert registry.is_registered("integration_test_graph")
