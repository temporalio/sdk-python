"""Tests for LangGraphPlugin validation."""

from __future__ import annotations

from langchain_core.runnables import RunnableLambda
from langgraph.graph import START, StateGraph  # pyright: ignore[reportMissingTypeStubs]
from pytest import raises
from typing_extensions import TypedDict

from temporalio.contrib.langgraph.langgraph_plugin import LangGraphPlugin


class State(TypedDict):
    value: str


async def async_node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    return {"value": "done"}


def sync_node(state: State) -> dict[str, str]:  # pyright: ignore[reportUnusedParameter]
    return {"value": "done"}


def test_non_runnable_callable_node_raises() -> None:
    """Nodes whose runnable isn't a RunnableCallable can't be wrapped as activities."""
    g: StateGraph[State, None, State, State] = StateGraph(State)
    g.add_node("node", RunnableLambda(sync_node))
    g.add_edge(START, "node")

    with raises(ValueError, match="must have an async function"):
        LangGraphPlugin(graphs=[g])


def test_invalid_execute_in_raises() -> None:
    g: StateGraph[State, None, State, State] = StateGraph(State)
    g.add_node("node", async_node, metadata={"execute_in": "bogus"})
    g.add_edge(START, "node")

    with raises(ValueError, match="Invalid execute_in value"):
        LangGraphPlugin(graphs=[g])
