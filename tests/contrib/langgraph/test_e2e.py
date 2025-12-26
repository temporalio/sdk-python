"""End-to-end tests for LangGraph-Temporal integration.

These tests run actual workflows with real Temporal workers to verify
the complete interrupt/resume flow works correctly.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest
from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph
from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin

from tests.contrib.langgraph.e2e_workflows import (
    ApprovalWorkflow,
    MultiInterruptWorkflow,
    MultiInvokeStoreWorkflow,
    RejectionWorkflow,
    SimpleGraphWorkflow,
    StoreWorkflow,
)
from tests.helpers import new_worker


# ==============================================================================
# Graph State Types
# ==============================================================================


class SimpleState(TypedDict, total=False):
    """State for simple workflow without interrupts."""

    value: int
    result: int


class ApprovalState(TypedDict, total=False):
    """State for approval workflow."""

    value: int
    approved: bool
    approval_reason: str


class MultiInterruptState(TypedDict, total=False):
    """State for multi-interrupt workflow."""

    value: int
    step1_result: str
    step2_result: str


class StoreState(TypedDict, total=False):
    """State for store test workflow."""

    user_id: str
    node1_read: str | None
    node2_read: str | None


class MultiInvokeStoreState(TypedDict, total=False):
    """State for multi-invocation store test workflow."""

    user_id: str
    invocation_num: int
    previous_count: int | None
    current_count: int | None


# ==============================================================================
# Graph Node Functions
# ==============================================================================


def double_node(state: SimpleState) -> SimpleState:
    """Simple node that doubles the value."""
    return {"result": state.get("value", 0) * 2}


def approval_node(state: ApprovalState) -> ApprovalState:
    """Node that requests approval via interrupt."""
    from langgraph.types import interrupt

    approval_response = interrupt({
        "question": "Do you approve this value?",
        "current_value": state.get("value", 0),
    })

    return {
        "approved": approval_response.get("approved", False),
        "approval_reason": approval_response.get("reason", ""),
    }


def process_node(state: ApprovalState) -> ApprovalState:
    """Node that processes the approved value."""
    if state.get("approved"):
        return {"value": state.get("value", 0) * 2}
    return {"value": 0}


def step1_node(state: MultiInterruptState) -> MultiInterruptState:
    """First step that requires human input."""
    from langgraph.types import interrupt

    response = interrupt({"step": 1, "question": "Enter value for step 1"})
    return {"step1_result": str(response)}


def step2_node(state: MultiInterruptState) -> MultiInterruptState:
    """Second step that requires human input."""
    from langgraph.types import interrupt

    response = interrupt({"step": 2, "question": "Enter value for step 2"})
    return {"step2_result": str(response)}


def store_node1(state: StoreState) -> StoreState:
    """Node that writes to store and reads from it."""
    from langgraph.config import get_store

    store = get_store()
    user_id = state.get("user_id", "default")

    # Try to read existing value (should be None on first run)
    existing = store.get(("user", user_id), "preferences")
    existing_value = existing.value["theme"] if existing else None

    # Write a new value to the store
    store.put(("user", user_id), "preferences", {"theme": "dark", "written_by": "node1"})

    return {"node1_read": existing_value}


def store_node2(state: StoreState) -> StoreState:
    """Node that reads from store (should see node1's write)."""
    from langgraph.config import get_store

    store = get_store()
    user_id = state.get("user_id", "default")

    # Read the value written by node1
    item = store.get(("user", user_id), "preferences")
    read_value = item.value["theme"] if item else None

    return {"node2_read": read_value}


def counter_node(state: MultiInvokeStoreState) -> MultiInvokeStoreState:
    """Node that increments a counter in the store.

    Each invocation reads the previous count and increments it.
    This tests that store data persists across graph invocations.
    """
    from langgraph.config import get_store

    store = get_store()
    user_id = state.get("user_id", "default")

    # Read existing count
    item = store.get(("counters", user_id), "invocation_count")
    previous_count = item.value["count"] if item else 0

    # Increment and write new count
    new_count = previous_count + 1
    store.put(("counters", user_id), "invocation_count", {"count": new_count})

    return {
        "previous_count": previous_count if previous_count > 0 else None,
        "current_count": new_count,
    }


# ==============================================================================
# Graph Builder Functions
# ==============================================================================


def build_simple_graph():
    """Build a simple graph without interrupts."""
    graph = StateGraph(SimpleState)
    graph.add_node("double", double_node)
    graph.add_edge(START, "double")
    graph.add_edge("double", END)
    return graph.compile()


def build_approval_graph():
    """Build the approval graph with interrupt."""
    graph = StateGraph(ApprovalState)
    graph.add_node("request_approval", approval_node)
    graph.add_node("process", process_node)
    graph.add_edge(START, "request_approval")
    graph.add_edge("request_approval", "process")
    graph.add_edge("process", END)
    return graph.compile()


def build_multi_interrupt_graph():
    """Build a graph with multiple sequential interrupts."""
    graph = StateGraph(MultiInterruptState)
    graph.add_node("step1", step1_node)
    graph.add_node("step2", step2_node)
    graph.add_edge(START, "step1")
    graph.add_edge("step1", "step2")
    graph.add_edge("step2", END)
    return graph.compile()


def build_store_graph():
    """Build a graph that uses store for cross-node persistence."""
    graph = StateGraph(StoreState)
    graph.add_node("node1", store_node1)
    graph.add_node("node2", store_node2)
    graph.add_edge(START, "node1")
    graph.add_edge("node1", "node2")
    graph.add_edge("node2", END)
    return graph.compile()


def build_counter_graph():
    """Build a graph that increments a counter in the store.

    Used to test store persistence across multiple graph invocations.
    """
    graph = StateGraph(MultiInvokeStoreState)
    graph.add_node("counter", counter_node)
    graph.add_edge(START, "counter")
    graph.add_edge("counter", END)
    return graph.compile()


# ==============================================================================
# Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_simple_graph_execution(client: Client) -> None:
    """Test basic graph execution without interrupts."""
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    # Clear registry to avoid conflicts
    get_global_registry().clear()

    # Create plugin with the graph
    plugin = LangGraphPlugin(
        graphs={"e2e_simple": build_simple_graph},
        default_activity_timeout=timedelta(seconds=30),
    )

    # Apply plugin to client
    new_config = client.config()
    existing_plugins = new_config.get("plugins", [])
    new_config["plugins"] = list(existing_plugins) + [plugin]
    plugin_client = Client(**new_config)

    # Run workflow (plugin is already applied to client)
    async with new_worker(
        plugin_client,
        SimpleGraphWorkflow,
    ) as worker:
        result = await plugin_client.execute_workflow(
            SimpleGraphWorkflow.run,
            21,
            id=f"e2e-simple-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )

        assert result["result"] == 42


@pytest.mark.asyncio
async def test_interrupt_and_resume_with_signal(client: Client) -> None:
    """Test interrupt flow with signal-based resume."""
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    # Clear registry to avoid conflicts
    get_global_registry().clear()

    # Create plugin with the approval graph
    plugin = LangGraphPlugin(
        graphs={"e2e_approval": build_approval_graph},
        default_activity_timeout=timedelta(seconds=30),
    )

    # Apply plugin to client
    new_config = client.config()
    existing_plugins = new_config.get("plugins", [])
    new_config["plugins"] = list(existing_plugins) + [plugin]
    plugin_client = Client(**new_config)

    # Run workflow
    async with new_worker(
        plugin_client,
        ApprovalWorkflow,
    ) as worker:
        # Start workflow
        handle = await plugin_client.start_workflow(
            ApprovalWorkflow.run,
            42,
            id=f"e2e-approval-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=60),
        )

        # Wait for the workflow to reach the interrupt
        await asyncio.sleep(1)

        # Query the interrupt value
        interrupt_value = await handle.query(ApprovalWorkflow.get_interrupt_value)
        assert interrupt_value is not None
        assert interrupt_value["question"] == "Do you approve this value?"
        assert interrupt_value["current_value"] == 42

        # Send approval signal
        await handle.signal(
            ApprovalWorkflow.provide_approval,
            {"approved": True, "reason": "Looks good!"},
        )

        # Wait for workflow completion
        result = await handle.result()

        # Value should be doubled (42 * 2 = 84)
        assert result["value"] == 84
        assert result["approved"] is True
        assert result["approval_reason"] == "Looks good!"


@pytest.mark.asyncio
async def test_interrupt_with_rejection(client: Client) -> None:
    """Test interrupt flow where approval is rejected."""
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    # Clear registry to avoid conflicts
    get_global_registry().clear()

    # Create plugin with the approval graph
    plugin = LangGraphPlugin(
        graphs={"e2e_approval_reject": build_approval_graph},
        default_activity_timeout=timedelta(seconds=30),
    )

    # Apply plugin to client
    new_config = client.config()
    existing_plugins = new_config.get("plugins", [])
    new_config["plugins"] = list(existing_plugins) + [plugin]
    plugin_client = Client(**new_config)

    async with new_worker(
        plugin_client,
        RejectionWorkflow,
    ) as worker:
        handle = await plugin_client.start_workflow(
            RejectionWorkflow.run,
            100,
            id=f"e2e-reject-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=60),
        )

        await asyncio.sleep(1)

        # Reject the approval
        await handle.signal(
            RejectionWorkflow.provide_approval,
            {"approved": False, "reason": "Not approved"},
        )

        result = await handle.result()

        # Value should be 0 (rejected)
        assert result["value"] == 0
        assert result["approved"] is False


@pytest.mark.asyncio
async def test_multiple_sequential_interrupts(client: Client) -> None:
    """Test workflow that handles multiple interrupts in sequence."""
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    # Clear registry to avoid conflicts
    get_global_registry().clear()

    # Create plugin with the multi-interrupt graph
    plugin = LangGraphPlugin(
        graphs={"e2e_multi_interrupt": build_multi_interrupt_graph},
        default_activity_timeout=timedelta(seconds=30),
    )

    # Apply plugin to client
    new_config = client.config()
    existing_plugins = new_config.get("plugins", [])
    new_config["plugins"] = list(existing_plugins) + [plugin]
    plugin_client = Client(**new_config)

    async with new_worker(
        plugin_client,
        MultiInterruptWorkflow,
    ) as worker:
        handle = await plugin_client.start_workflow(
            MultiInterruptWorkflow.run,
            {"value": 100},
            id=f"e2e-multi-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=60),
        )

        # Wait for first interrupt
        await asyncio.sleep(1)

        # Verify first interrupt
        interrupt_count = await handle.query(MultiInterruptWorkflow.get_interrupt_count)
        assert interrupt_count == 1

        current_interrupt = await handle.query(MultiInterruptWorkflow.get_current_interrupt)
        assert current_interrupt["step"] == 1

        # Check invocation_id before signal
        invocation_id = await handle.query(MultiInterruptWorkflow.get_invocation_id)
        assert invocation_id == 1, f"Expected invocation_id=1 before signal, got {invocation_id}"

        # Respond to first interrupt
        await handle.signal(MultiInterruptWorkflow.provide_response, "first_value")

        # Wait for second interrupt
        await asyncio.sleep(1)

        # Debug: check invocation_id after signal
        invocation_id_after = await handle.query(MultiInterruptWorkflow.get_invocation_id)
        debug_info = await handle.query(MultiInterruptWorkflow.get_debug_info)
        print(f"invocation_id after signal: {invocation_id_after}")
        print(f"debug_info: {debug_info}")

        # Verify second interrupt
        interrupt_count = await handle.query(MultiInterruptWorkflow.get_interrupt_count)
        assert interrupt_count == 2, f"Expected interrupt_count=2, got {interrupt_count}. invocation_id={invocation_id_after}. debug={debug_info}"

        current_interrupt = await handle.query(MultiInterruptWorkflow.get_current_interrupt)
        assert current_interrupt["step"] == 2

        # Respond to second interrupt
        await handle.signal(MultiInterruptWorkflow.provide_response, "second_value")

        # Wait for completion
        result = await handle.result()

        # Verify final result
        assert result["step1_result"] == "first_value"
        assert result["step2_result"] == "second_value"


@pytest.mark.asyncio
async def test_store_persistence(client: Client) -> None:
    """Test that store data persists across node executions."""
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    # Clear registry to avoid conflicts
    get_global_registry().clear()

    # Create plugin with the store graph
    plugin = LangGraphPlugin(
        graphs={"e2e_store": build_store_graph},
        default_activity_timeout=timedelta(seconds=30),
    )

    # Apply plugin to client
    new_config = client.config()
    existing_plugins = new_config.get("plugins", [])
    new_config["plugins"] = list(existing_plugins) + [plugin]
    plugin_client = Client(**new_config)

    async with new_worker(
        plugin_client,
        StoreWorkflow,
    ) as worker:
        result = await plugin_client.execute_workflow(
            StoreWorkflow.run,
            "test_user_123",
            id=f"e2e-store-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )

        # Node1 should read None (no prior data)
        assert result["node1_read"] is None

        # Node2 should read the value written by Node1
        assert result["node2_read"] == "dark"


@pytest.mark.asyncio
async def test_store_persistence_across_invocations(client: Client) -> None:
    """Test that store data persists across multiple graph invocations.

    This verifies that when the same graph is invoked multiple times within
    a workflow, store data written in earlier invocations is visible to
    later invocations.
    """
    from temporalio.contrib.langgraph._graph_registry import get_global_registry

    # Clear registry to avoid conflicts
    get_global_registry().clear()

    # Create plugin with the counter graph
    plugin = LangGraphPlugin(
        graphs={"e2e_counter": build_counter_graph},
        default_activity_timeout=timedelta(seconds=30),
    )

    # Apply plugin to client
    new_config = client.config()
    existing_plugins = new_config.get("plugins", [])
    new_config["plugins"] = list(existing_plugins) + [plugin]
    plugin_client = Client(**new_config)

    async with new_worker(
        plugin_client,
        MultiInvokeStoreWorkflow,
    ) as worker:
        # Run the graph 3 times within the same workflow
        results = await plugin_client.execute_workflow(
            MultiInvokeStoreWorkflow.run,
            args=["test_user_456", 3],
            id=f"e2e-multi-invoke-store-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )

        # Should have 3 results
        assert len(results) == 3

        # First invocation: previous_count=None, current_count=1
        assert results[0]["previous_count"] is None
        assert results[0]["current_count"] == 1

        # Second invocation: previous_count=1, current_count=2
        assert results[1]["previous_count"] == 1
        assert results[1]["current_count"] == 2

        # Third invocation: previous_count=2, current_count=3
        assert results[2]["previous_count"] == 2
        assert results[2]["current_count"] == 3
