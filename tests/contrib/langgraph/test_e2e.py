"""End-to-end tests for LangGraph-Temporal integration.

These tests run actual workflows with real Temporal workers to verify
the complete integration works correctly.

Test organization:
- TestBasicExecution: Simple graph execution without interrupts
- TestInterrupts: Human-in-the-loop interrupt tests
- TestStore: Store persistence tests
- TestAdvancedFeatures: Send API, subgraphs, Command goto
- TestAgenticWorkflows: React agent with temporal tools
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from temporalio.client import Client
from temporalio.contrib.langgraph import LangGraphPlugin

from tests.contrib.langgraph.e2e_graphs import (
    build_approval_graph,
    build_command_graph,
    build_counter_graph,
    build_multi_interrupt_graph,
    build_react_agent_graph,
    build_send_graph,
    build_simple_graph,
    build_store_graph,
    build_subgraph,
)
from tests.contrib.langgraph.e2e_workflows import (
    ApprovalE2EWorkflow,
    CommandE2EWorkflow,
    MultiInterruptE2EWorkflow,
    MultiInvokeStoreE2EWorkflow,
    ReactAgentE2EWorkflow,
    RejectionE2EWorkflow,
    SendE2EWorkflow,
    SimpleE2EWorkflow,
    StoreE2EWorkflow,
    SubgraphE2EWorkflow,
)
from tests.helpers import new_worker


# ==============================================================================
# Basic Execution Tests
# ==============================================================================


class TestBasicExecution:
    """Tests for basic graph execution without interrupts."""

    @pytest.mark.asyncio
    async def test_simple_graph_execution(self, client: Client) -> None:
        """Test basic graph execution without interrupts."""
        plugin = LangGraphPlugin(
            graphs={"e2e_simple": build_simple_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, SimpleE2EWorkflow) as worker:
            result = await plugin_client.execute_workflow(
                SimpleE2EWorkflow.run,
                21,
                id=f"e2e-simple-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            assert result["result"] == 42


# ==============================================================================
# Interrupt Tests
# ==============================================================================


class TestInterrupts:
    """Tests for human-in-the-loop interrupt functionality."""

    @pytest.mark.asyncio
    async def test_interrupt_and_resume_with_signal(self, client: Client) -> None:
        """Test interrupt flow with signal-based resume."""
        plugin = LangGraphPlugin(
            graphs={"e2e_approval": build_approval_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, ApprovalE2EWorkflow) as worker:
            handle = await plugin_client.start_workflow(
                ApprovalE2EWorkflow.run,
                42,
                id=f"e2e-approval-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )

            # Wait for the workflow to reach the interrupt
            await asyncio.sleep(1)

            # Query the interrupt value
            interrupt_value = await handle.query(
                ApprovalE2EWorkflow.get_interrupt_value
            )
            assert interrupt_value is not None
            assert interrupt_value["question"] == "Do you approve this value?"
            assert interrupt_value["current_value"] == 42

            # Send approval signal
            await handle.signal(
                ApprovalE2EWorkflow.provide_approval,
                {"approved": True, "reason": "Looks good!"},
            )

            # Wait for workflow completion
            result = await handle.result()

            # Value should be doubled (42 * 2 = 84)
            assert result["value"] == 84
            assert result["approved"] is True
            assert result["approval_reason"] == "Looks good!"

    @pytest.mark.asyncio
    async def test_interrupt_with_rejection(self, client: Client) -> None:
        """Test interrupt flow where approval is rejected."""
        # Use a different graph ID to avoid registry conflicts
        plugin = LangGraphPlugin(
            graphs={"e2e_rejection": build_approval_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, RejectionE2EWorkflow) as worker:
            handle = await plugin_client.start_workflow(
                RejectionE2EWorkflow.run,
                100,
                id=f"e2e-reject-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )

            await asyncio.sleep(1)

            # Reject the approval
            await handle.signal(
                RejectionE2EWorkflow.provide_approval,
                {"approved": False, "reason": "Not approved"},
            )

            result = await handle.result()

            # Value should be 0 (rejected)
            assert result["value"] == 0
            assert result["approved"] is False

    @pytest.mark.asyncio
    async def test_multiple_sequential_interrupts(self, client: Client) -> None:
        """Test workflow that handles multiple interrupts in sequence."""
        plugin = LangGraphPlugin(
            graphs={"e2e_multi_interrupt": build_multi_interrupt_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, MultiInterruptE2EWorkflow) as worker:
            handle = await plugin_client.start_workflow(
                MultiInterruptE2EWorkflow.run,
                {"value": 100},
                id=f"e2e-multi-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )

            # Wait for first interrupt
            await asyncio.sleep(1)

            # Verify first interrupt
            interrupt_count = await handle.query(
                MultiInterruptE2EWorkflow.get_interrupt_count
            )
            assert interrupt_count == 1

            current_interrupt = await handle.query(
                MultiInterruptE2EWorkflow.get_current_interrupt
            )
            assert current_interrupt["step"] == 1

            # Check invocation_id before signal
            invocation_id = await handle.query(
                MultiInterruptE2EWorkflow.get_invocation_id
            )
            assert (
                invocation_id == 1
            ), f"Expected invocation_id=1 before signal, got {invocation_id}"

            # Respond to first interrupt
            await handle.signal(
                MultiInterruptE2EWorkflow.provide_response, "first_value"
            )

            # Wait for second interrupt
            await asyncio.sleep(1)

            # Debug: check invocation_id after signal
            invocation_id_after = await handle.query(
                MultiInterruptE2EWorkflow.get_invocation_id
            )
            debug_info = await handle.query(MultiInterruptE2EWorkflow.get_debug_info)
            print(f"invocation_id after signal: {invocation_id_after}")
            print(f"debug_info: {debug_info}")

            # Verify second interrupt
            interrupt_count = await handle.query(
                MultiInterruptE2EWorkflow.get_interrupt_count
            )
            assert (
                interrupt_count == 2
            ), f"Expected interrupt_count=2, got {interrupt_count}. invocation_id={invocation_id_after}. debug={debug_info}"

            current_interrupt = await handle.query(
                MultiInterruptE2EWorkflow.get_current_interrupt
            )
            assert current_interrupt["step"] == 2

            # Respond to second interrupt
            await handle.signal(
                MultiInterruptE2EWorkflow.provide_response, "second_value"
            )

            # Wait for completion
            result = await handle.result()

            # Verify final result
            assert result["step1_result"] == "first_value"
            assert result["step2_result"] == "second_value"


# ==============================================================================
# Store Tests
# ==============================================================================


class TestStore:
    """Tests for store persistence functionality."""

    @pytest.mark.asyncio
    async def test_store_persistence(self, client: Client) -> None:
        """Test that store data persists across node executions."""
        plugin = LangGraphPlugin(
            graphs={"e2e_store": build_store_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, StoreE2EWorkflow) as worker:
            result = await plugin_client.execute_workflow(
                StoreE2EWorkflow.run,
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
    async def test_store_persistence_across_invocations(self, client: Client) -> None:
        """Test that store data persists across multiple graph invocations.

        This verifies that when the same graph is invoked multiple times within
        a workflow, store data written in earlier invocations is visible to
        later invocations.
        """
        plugin = LangGraphPlugin(
            graphs={"e2e_counter": build_counter_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, MultiInvokeStoreE2EWorkflow) as worker:
            # Run the graph 3 times within the same workflow
            results = await plugin_client.execute_workflow(
                MultiInvokeStoreE2EWorkflow.run,
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


# ==============================================================================
# Advanced Feature Tests
# ==============================================================================


class TestAdvancedFeatures:
    """Tests for advanced LangGraph features."""

    @pytest.mark.asyncio
    async def test_send_api_dynamic_parallelism(self, client: Client) -> None:
        """Test that Send API creates dynamic parallel tasks."""
        plugin = LangGraphPlugin(
            graphs={"e2e_send": build_send_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, SendE2EWorkflow) as worker:
            result = await plugin_client.execute_workflow(
                SendE2EWorkflow.run,
                [1, 2, 3, 4, 5],
                id=f"e2e-send-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            # Items [1, 2, 3, 4, 5] should be doubled to [2, 4, 6, 8, 10]
            # Results are accumulated via operator.add
            assert sorted(result.get("results", [])) == [2, 4, 6, 8, 10]

    @pytest.mark.asyncio
    async def test_subgraph_execution(self, client: Client) -> None:
        """Test that subgraphs execute correctly."""
        plugin = LangGraphPlugin(
            graphs={"e2e_subgraph": build_subgraph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, SubgraphE2EWorkflow) as worker:
            result = await plugin_client.execute_workflow(
                SubgraphE2EWorkflow.run,
                5,
                id=f"e2e-subgraph-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            # value=5 -> parent_start adds 10 -> value=15
            # child_process multiplies by 3 -> child_result=45
            # parent_end adds 100 -> final_result=145
            assert result.get("final_result") == 145

    @pytest.mark.asyncio
    async def test_command_goto_skip_node(self, client: Client) -> None:
        """Test that Command(goto=) can skip nodes."""
        plugin = LangGraphPlugin(
            graphs={"e2e_command": build_command_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, CommandE2EWorkflow) as worker:
            # Test with value > 10 (should skip middle node)
            result = await plugin_client.execute_workflow(
                CommandE2EWorkflow.run,
                20,
                id=f"e2e-command-skip-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            # value=20 > 10, so Command(goto="finish") skips middle
            # Path should be: start -> finish (no middle)
            assert result.get("path") == ["start", "finish"]
            # Result should be 20 + 1000 = 1020
            assert result.get("result") == 1020

    @pytest.mark.asyncio
    async def test_command_goto_normal_path(self, client: Client) -> None:
        """Test that Command(goto=) follows normal path when condition not met."""
        plugin = LangGraphPlugin(
            graphs={"e2e_command": build_command_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, CommandE2EWorkflow) as worker:
            # Test with value <= 10 (should go through middle)
            result = await plugin_client.execute_workflow(
                CommandE2EWorkflow.run,
                5,
                id=f"e2e-command-normal-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            # value=5 <= 10, so Command(goto="middle")
            # Path should be: start -> middle -> finish
            assert result.get("path") == ["start", "middle", "finish"]
            # value=5 -> middle doubles to 10 -> finish adds 1000 = 1010
            assert result.get("result") == 1010


# ==============================================================================
# Agentic Workflow Tests
# ==============================================================================


class TestAgenticWorkflows:
    """Tests for agentic workflows with tools and models."""

    @pytest.mark.asyncio
    async def test_react_agent_with_temporal_tool(self, client: Client) -> None:
        """Test react agent using temporal_tool for durable tool execution."""
        plugin = LangGraphPlugin(
            graphs={"e2e_react_agent": build_react_agent_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        async with new_worker(plugin_client, ReactAgentE2EWorkflow) as worker:
            result = await plugin_client.execute_workflow(
                ReactAgentE2EWorkflow.run,
                "What is 2 + 2?",
                id=f"e2e-react-agent-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )

            # Verify the agent produced a result
            assert (
                result["message_count"] >= 3
            )  # Human, AI (tool call), Tool, AI (answer)
            assert "4" in result["answer"]  # Should contain the calculation result

    @pytest.mark.asyncio
    async def test_tools_node_activity_summary_shows_tool_calls(
        self, client: Client
    ) -> None:
        """Test that tools node activity summary shows tool name and args."""
        plugin = LangGraphPlugin(
            graphs={"e2e_react_agent": build_react_agent_graph},
            default_activity_timeout=timedelta(seconds=30),
        )

        new_config = client.config()
        existing_plugins = new_config.get("plugins", [])
        new_config["plugins"] = list(existing_plugins) + [plugin]
        plugin_client = Client(**new_config)

        workflow_id = f"e2e-react-summary-{uuid.uuid4()}"

        async with new_worker(plugin_client, ReactAgentE2EWorkflow) as worker:
            await plugin_client.execute_workflow(
                ReactAgentE2EWorkflow.run,
                "What is 2 + 2?",
                id=workflow_id,
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )

        # Get workflow history and check activity summaries
        handle = plugin_client.get_workflow_handle(workflow_id)
        history = await handle.fetch_history()

        # Find ActivityTaskScheduled events and check their summaries and types
        activity_summaries: list[str] = []
        activity_types: dict[str, str] = {}  # summary -> activity type
        for event in history.events:
            if event.HasField("activity_task_scheduled_event_attributes"):
                attrs = event.activity_task_scheduled_event_attributes
                activity_type = attrs.activity_type.name
                # user_metadata is on the HistoryEvent, not on the attributes
                if event.HasField("user_metadata") and event.user_metadata.summary.data:
                    summary = event.user_metadata.summary.data.decode("utf-8")
                    activity_summaries.append(summary)
                    activity_types[summary] = activity_type

        # Verify we have activity summaries
        assert len(activity_summaries) > 0, "No activity summaries found"

        # Find the tools node activity - should show tool call info
        # The fake model calls calculator({'expression': '2 + 2'})
        tools_summaries = [s for s in activity_summaries if "calculator" in s]
        assert (
            len(tools_summaries) > 0
        ), f"Expected 'calculator' in summaries, got: {activity_summaries}"

        # Verify the summary contains the args
        assert any(
            "expression" in s and "2 + 2" in s for s in tools_summaries
        ), f"Expected tool args in summary, got: {tools_summaries}"

        # Verify the tool node uses tool_node activity type
        for tool_summary in tools_summaries:
            assert activity_types[tool_summary] == "tool_node", (
                f"Expected tool_node activity type for tool, "
                f"got: {activity_types[tool_summary]}"
            )
