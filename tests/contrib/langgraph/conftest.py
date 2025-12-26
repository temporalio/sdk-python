"""Shared pytest fixtures for LangGraph tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def clear_graph_registry():
    """Clear the global graph registry before each test.

    This ensures tests don't interfere with each other through the global registry.
    """
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    get_global_registry().clear()
    yield
    get_global_registry().clear()


@pytest.fixture(autouse=True)
def clear_tool_registry():
    """Clear the global tool registry before each test."""
    from temporalio.contrib.langgraph._tool_registry import clear_registry

    clear_registry()
    yield
    clear_registry()


@pytest.fixture(autouse=True)
def clear_model_registry():
    """Clear the global model registry before each test."""
    from temporalio.contrib.langgraph._model_registry import clear_registry

    clear_registry()
    yield
    clear_registry()
