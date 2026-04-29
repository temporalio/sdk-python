"""Tests for LangGraphPlugin validation."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from langchain_core.runnables import RunnableLambda
from langgraph.func import task  # pyright: ignore[reportMissingTypeStubs]
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from langgraph.types import RetryPolicy  # pyright: ignore[reportMissingTypeStubs]
from pytest import raises
from typing_extensions import TypedDict

from temporalio.contrib.langgraph import LangGraphPlugin


class State(TypedDict):
    value: str


async def async_node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    return {"value": "done"}


def sync_node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    return {"value": "done"}


def test_non_runnable_callable_node_raises() -> None:
    """Nodes whose runnable isn't a RunnableCallable can't be wrapped as activities."""
    g = StateGraph(State)
    g.add_node("node", RunnableLambda(sync_node))
    g.add_edge(START, "node")

    with raises(ValueError, match="must be a RunnableCallable"):
        LangGraphPlugin(graphs={f"validation-{uuid4()}": g})


def test_invalid_execute_in_raises() -> None:
    g = StateGraph(State)
    g.add_node("node", async_node, metadata={"execute_in": "bogus"})
    g.add_edge(START, "node")

    with raises(ValueError, match="Invalid execute_in value"):
        LangGraphPlugin(graphs={f"validation-{uuid4()}": g})


def test_graph_node_missing_execute_in_raises() -> None:
    g = StateGraph(State)
    g.add_node("node", async_node)
    g.add_edge(START, "node")

    with raises(ValueError, match="missing required 'execute_in'"):
        LangGraphPlugin(graphs={f"validation-{uuid4()}": g})


def test_functional_task_missing_execute_in_raises() -> None:
    @task
    def my_task(x: int) -> int:
        return x + 1

    with raises(ValueError, match="missing required 'execute_in'"):
        LangGraphPlugin(tasks=[my_task])


def test_execute_in_in_default_activity_options_raises() -> None:
    with raises(ValueError, match="cannot be set in default_activity_options"):
        LangGraphPlugin(default_activity_options={"execute_in": "activity"})


def test_node_retry_policy_raises() -> None:
    g = StateGraph(State)
    g.add_node("node", async_node, retry_policy=RetryPolicy(max_attempts=3))
    g.add_edge(START, "node")

    with raises(ValueError, match="retry_policy"):
        LangGraphPlugin(graphs={f"validation-{uuid4()}": g})


def test_task_retry_policy_raises() -> None:
    decorator: Any = task(retry_policy=RetryPolicy(max_attempts=3))

    @decorator
    def my_task(x: int) -> int:
        return x + 1

    with raises(ValueError, match="retry_policy"):
        LangGraphPlugin(tasks=[my_task])
