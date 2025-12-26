"""Validation tests for LangGraph features that need verification.

These tests validate that advanced LangGraph features work correctly
with the Temporal integration.
"""

from __future__ import annotations

import operator
import uuid
from datetime import timedelta
from typing import Annotated, Any

import pytest
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin

from tests.helpers import new_worker

# Use imports_passed_through for langgraph imports
with workflow.unsafe.imports_passed_through():
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, Send


# ==============================================================================
# Test 1: Send API / Dynamic Parallelism
# ==============================================================================


class SendState(TypedDict, total=False):
    """State for Send API test."""
    items: list[int]
    results: Annotated[list[int], operator.add]


def setup_node(state: SendState) -> SendState:
    """Setup node that just passes through."""
    return {}


def continue_to_workers(state: SendState) -> list[Send]:
    """Conditional edge function that creates parallel worker tasks via Send."""
    items = state.get("items", [])
    # Return a list of Send objects to create parallel tasks
    return [Send("worker", {"item": item}) for item in items]


def worker_node(state: dict) -> dict:
    """Worker node that processes a single item."""
    item = state.get("item", 0)
    # Double the item
    return {"results": [item * 2]}


def build_send_graph():
    """Build a graph that uses Send for dynamic parallelism."""
    graph = StateGraph(SendState)
    graph.add_node("setup", setup_node)
    graph.add_node("worker", worker_node)
    graph.add_edge(START, "setup")
    # Send API: conditional edge function returns list of Send objects
    graph.add_conditional_edges("setup", continue_to_workers, ["worker"])
    graph.add_edge("worker", END)
    return graph.compile()


with workflow.unsafe.imports_passed_through():
    from temporalio.contrib.langgraph import compile as lg_compile


@workflow.defn
class SendWorkflow:
    """Workflow that tests Send API."""

    @workflow.run
    async def run(self, items: list[int]) -> dict:
        app = lg_compile("validation_send")
        return await app.ainvoke({"items": items})


# ==============================================================================
# Test 2: Subgraphs / Nested Graphs
# ==============================================================================


class ParentState(TypedDict, total=False):
    """State for parent graph."""
    value: int
    child_result: int
    final_result: int


class ChildState(TypedDict, total=False):
    """State for child subgraph."""
    value: int
    child_result: int


def parent_start_node(state: ParentState) -> ParentState:
    """Parent node that prepares state for child."""
    return {"value": state.get("value", 0) + 10}


def child_process_node(state: ChildState) -> ChildState:
    """Child node that processes the value."""
    return {"child_result": state.get("value", 0) * 3}


def parent_end_node(state: ParentState) -> ParentState:
    """Parent node that finalizes result."""
    return {"final_result": state.get("child_result", 0) + 100}


def build_subgraph():
    """Build a parent graph with a child subgraph."""
    # Create child subgraph
    child = StateGraph(ChildState)
    child.add_node("child_process", child_process_node)
    child.add_edge(START, "child_process")
    child.add_edge("child_process", END)
    child_compiled = child.compile()

    # Create parent graph with child as a node
    parent = StateGraph(ParentState)
    parent.add_node("parent_start", parent_start_node)
    parent.add_node("child_graph", child_compiled)
    parent.add_node("parent_end", parent_end_node)
    parent.add_edge(START, "parent_start")
    parent.add_edge("parent_start", "child_graph")
    parent.add_edge("child_graph", "parent_end")
    parent.add_edge("parent_end", END)
    return parent.compile()


@workflow.defn
class SubgraphWorkflow:
    """Workflow that tests subgraph execution."""

    @workflow.run
    async def run(self, value: int) -> dict:
        app = lg_compile("validation_subgraph")
        return await app.ainvoke({"value": value})


# ==============================================================================
# Test 3: Command API (goto)
# ==============================================================================


class CommandState(TypedDict, total=False):
    """State for Command goto test."""
    value: int
    path: Annotated[list[str], operator.add]  # Reducer to accumulate path entries
    result: int


def command_start_node(state: CommandState) -> Command:
    """Node that uses Command to navigate."""
    value = state.get("value", 0)

    # Use Command to update state AND goto specific node
    # With operator.add reducer, return only ["start"] - it will be accumulated
    if value > 10:
        # Jump to finish node, skipping middle
        return Command(
            goto="finish",
            update={"path": ["start"], "value": value},
        )
    else:
        # Go to middle node normally
        return Command(
            goto="middle",
            update={"path": ["start"], "value": value},
        )


def command_middle_node(state: CommandState) -> CommandState:
    """Middle node in the path."""
    # With operator.add reducer, return only ["middle"]
    return {"path": ["middle"], "value": state.get("value", 0) * 2}


def command_finish_node(state: CommandState) -> CommandState:
    """Final node that computes result."""
    # With operator.add reducer, return only ["finish"]
    return {"path": ["finish"], "result": state.get("value", 0) + 1000}


def build_command_graph():
    """Build a graph that uses Command for navigation.

    With Command, we don't add a static edge from 'start' - the Command(goto=...)
    determines where to go next. If we had both static edge and Command, both
    paths would execute.
    """
    graph = StateGraph(CommandState)
    graph.add_node("start", command_start_node)
    graph.add_node("middle", command_middle_node)
    graph.add_node("finish", command_finish_node)
    graph.add_edge(START, "start")
    # NO edge from start - Command(goto=...) handles the routing
    graph.add_edge("middle", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


@workflow.defn
class CommandWorkflow:
    """Workflow that tests Command goto API."""

    @workflow.run
    async def run(self, value: int) -> dict:
        app = lg_compile("validation_command")
        return await app.ainvoke({"value": value})


# ==============================================================================
# Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_send_api_dynamic_parallelism(client: Client) -> None:
    """Test that Send API creates dynamic parallel tasks."""
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    get_global_registry().clear()

    plugin = LangGraphPlugin(
        graphs={"validation_send": build_send_graph},
        default_activity_timeout=timedelta(seconds=30),
    )

    new_config = client.config()
    existing_plugins = new_config.get("plugins", [])
    new_config["plugins"] = list(existing_plugins) + [plugin]
    plugin_client = Client(**new_config)

    async with new_worker(plugin_client, SendWorkflow) as worker:
        result = await plugin_client.execute_workflow(
            SendWorkflow.run,
            [1, 2, 3, 4, 5],
            id=f"validation-send-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )

        # Items [1, 2, 3, 4, 5] should be doubled to [2, 4, 6, 8, 10]
        # Results are accumulated via operator.add
        assert sorted(result.get("results", [])) == [2, 4, 6, 8, 10]


@pytest.mark.asyncio
async def test_subgraph_execution(client: Client) -> None:
    """Test that subgraphs execute correctly."""
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    get_global_registry().clear()

    plugin = LangGraphPlugin(
        graphs={"validation_subgraph": build_subgraph},
        default_activity_timeout=timedelta(seconds=30),
    )

    new_config = client.config()
    existing_plugins = new_config.get("plugins", [])
    new_config["plugins"] = list(existing_plugins) + [plugin]
    plugin_client = Client(**new_config)

    async with new_worker(plugin_client, SubgraphWorkflow) as worker:
        result = await plugin_client.execute_workflow(
            SubgraphWorkflow.run,
            5,
            id=f"validation-subgraph-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )

        # value=5 -> parent_start adds 10 -> value=15
        # child_process multiplies by 3 -> child_result=45
        # parent_end adds 100 -> final_result=145
        assert result.get("final_result") == 145


@pytest.mark.asyncio
async def test_command_goto_skip_node(client: Client) -> None:
    """Test that Command(goto=) can skip nodes."""
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    get_global_registry().clear()

    plugin = LangGraphPlugin(
        graphs={"validation_command": build_command_graph},
        default_activity_timeout=timedelta(seconds=30),
    )

    new_config = client.config()
    existing_plugins = new_config.get("plugins", [])
    new_config["plugins"] = list(existing_plugins) + [plugin]
    plugin_client = Client(**new_config)

    async with new_worker(plugin_client, CommandWorkflow) as worker:
        # Test with value > 10 (should skip middle node)
        result = await plugin_client.execute_workflow(
            CommandWorkflow.run,
            20,
            id=f"validation-command-skip-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )

        # value=20 > 10, so Command(goto="finish") skips middle
        # Path should be: start -> finish (no middle)
        assert result.get("path") == ["start", "finish"]
        # Result should be 20 + 1000 = 1020
        assert result.get("result") == 1020


@pytest.mark.asyncio
async def test_command_goto_normal_path(client: Client) -> None:
    """Test that Command(goto=) follows normal path when condition not met."""
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    get_global_registry().clear()

    plugin = LangGraphPlugin(
        graphs={"validation_command": build_command_graph},
        default_activity_timeout=timedelta(seconds=30),
    )

    new_config = client.config()
    existing_plugins = new_config.get("plugins", [])
    new_config["plugins"] = list(existing_plugins) + [plugin]
    plugin_client = Client(**new_config)

    async with new_worker(plugin_client, CommandWorkflow) as worker:
        # Test with value <= 10 (should go through middle)
        result = await plugin_client.execute_workflow(
            CommandWorkflow.run,
            5,
            id=f"validation-command-normal-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )

        # value=5 <= 10, so Command(goto="middle")
        # Path should be: start -> middle -> finish
        assert result.get("path") == ["start", "middle", "finish"]
        # value=5 -> middle doubles to 10 -> finish adds 1000 = 1010
        assert result.get("result") == 1010
