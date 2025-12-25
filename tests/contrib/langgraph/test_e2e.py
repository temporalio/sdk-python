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
    RejectionWorkflow,
    SimpleGraphWorkflow,
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


