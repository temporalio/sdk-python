"""Workflow definitions for LangGraph E2E tests.

All workflow classes used in E2E tests are defined here to ensure proper
sandbox compatibility. LangGraph imports are wrapped with imports_passed_through().

Naming conventions:
- Workflow classes: <Feature>E2EWorkflow
- Graph IDs referenced: e2e_<feature>
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from temporalio import workflow

# Use imports_passed_through for langgraph types used in workflows
with workflow.unsafe.imports_passed_through():
    from langgraph.types import Command

    from temporalio.contrib.langgraph import compile as lg_compile


# ==============================================================================
# Input Types
# ==============================================================================


@dataclass
class ContinueAsNewInput:
    """Input for ContinueAsNewE2EWorkflow."""

    input_value: int
    checkpoint: dict | None = None
    cycle_count: int = 0


# ==============================================================================
# Basic Execution Workflows
# ==============================================================================


@workflow.defn
class SimpleE2EWorkflow:
    """Simple workflow that runs a graph without interrupts."""

    @workflow.run
    async def run(self, input_value: int) -> dict:
        app = lg_compile("e2e_simple")
        return await app.ainvoke({"value": input_value})


# ==============================================================================
# Interrupt Workflows
# ==============================================================================


@workflow.defn
class ApprovalE2EWorkflow:
    """Workflow with interrupt for human approval.

    This demonstrates the full interrupt flow:
    1. Graph runs until interrupt() is called
    2. Workflow receives __interrupt__ in result
    3. Workflow waits for signal with human input
    4. Workflow resumes graph with Command(resume=value)
    """

    def __init__(self) -> None:
        self._approval_response: dict | None = None
        self._interrupt_value: Any = None

    @workflow.signal
    def provide_approval(self, response: dict) -> None:
        """Signal to provide approval response."""
        self._approval_response = response

    @workflow.query
    def get_interrupt_value(self) -> Any:
        """Query to get the current interrupt value."""
        return self._interrupt_value

    @workflow.run
    async def run(self, input_value: int) -> dict:
        app = lg_compile("e2e_approval")

        # First invocation - should hit interrupt
        result = await app.ainvoke({"value": input_value})

        # Check for interrupt (matches LangGraph native API)
        if "__interrupt__" in result:
            self._interrupt_value = result["__interrupt__"][0].value

            # Wait for signal with approval
            await workflow.wait_condition(lambda: self._approval_response is not None)

            # Resume with the approval response
            result = await app.ainvoke(Command(resume=self._approval_response))

        return result


@workflow.defn
class RejectionE2EWorkflow:
    """Workflow for testing interrupt rejection."""

    def __init__(self) -> None:
        self._approval_response: dict | None = None

    @workflow.signal
    def provide_approval(self, response: dict) -> None:
        """Signal to provide approval response."""
        self._approval_response = response

    @workflow.run
    async def run(self, input_value: int) -> dict:
        app = lg_compile("e2e_rejection")

        result = await app.ainvoke({"value": input_value})

        if "__interrupt__" in result:
            await workflow.wait_condition(lambda: self._approval_response is not None)
            result = await app.ainvoke(Command(resume=self._approval_response))

        return result


@workflow.defn
class MultiInterruptE2EWorkflow:
    """Workflow that handles multiple interrupts in sequence."""

    def __init__(self) -> None:
        self._response: Any = None
        self._interrupt_count: int = 0
        self._current_interrupt: Any = None
        self._invocation_id: int = 0
        self._app: Any = None

    @workflow.signal
    def provide_response(self, value: Any) -> None:
        """Signal to provide response for current interrupt."""
        self._response = value

    @workflow.query
    def get_interrupt_count(self) -> int:
        """Query to get number of interrupts handled."""
        return self._interrupt_count

    @workflow.query
    def get_current_interrupt(self) -> Any:
        """Query to get current interrupt value."""
        return self._current_interrupt

    @workflow.query
    def get_invocation_id(self) -> int:
        """Query to get current invocation ID."""
        return self._invocation_id

    @workflow.query
    def get_debug_info(self) -> dict:
        """Query to get debug info about runner state."""
        if self._app is None:
            return {"error": "no app"}
        return {
            "has_interrupted_state": self._app._interrupt.interrupted_state is not None,
            "interrupted_state": self._app._interrupt.interrupted_state,
            "interrupted_node": self._app._interrupt.interrupted_node_name,
            "completed_nodes": list(self._app._execution.completed_nodes_in_cycle),
            "resume_value": self._app._interrupt.resume_value,
            "resume_used": self._app._interrupt.resume_used,
            "pending_interrupt": self._app._interrupt.pending_interrupt,
        }

    @workflow.run
    async def run(self, input_state: dict) -> dict:
        self._app = lg_compile("e2e_multi_interrupt")
        app = self._app

        current_input: dict | Command = input_state

        while True:
            self._invocation_id += 1
            # Pass invocation_id in config to ensure unique activity IDs
            result = await app.ainvoke(
                current_input,
                config={"configurable": {"invocation_id": self._invocation_id}},
            )

            if "__interrupt__" not in result:
                return result

            self._interrupt_count += 1
            self._current_interrupt = result["__interrupt__"][0].value

            # Wait for human input
            await workflow.wait_condition(lambda: self._response is not None)

            # Resume with Command
            current_input = Command(resume=self._response)
            self._response = None


# ==============================================================================
# Store Workflows
# ==============================================================================


@workflow.defn
class StoreE2EWorkflow:
    """Workflow that tests store functionality across nodes.

    This tests that:
    1. Node1 can write to the store
    2. Node2 (in a subsequent activity) can read node1's writes
    3. Store data persists across node executions within a workflow
    """

    @workflow.run
    async def run(self, user_id: str) -> dict:
        app = lg_compile("e2e_store")
        return await app.ainvoke({"user_id": user_id})


@workflow.defn
class MultiInvokeStoreE2EWorkflow:
    """Workflow that invokes the same graph multiple times.

    This tests that store data persists across multiple ainvoke() calls
    within the same workflow execution.
    """

    @workflow.run
    async def run(self, user_id: str, num_invocations: int) -> list[dict]:
        """Run the counter graph multiple times."""
        app = lg_compile("e2e_counter")
        results = []

        for i in range(num_invocations):
            result = await app.ainvoke(
                {
                    "user_id": user_id,
                    "invocation_num": i + 1,
                }
            )
            results.append(result)

        return results


# ==============================================================================
# Advanced Feature Workflows
# ==============================================================================


@workflow.defn
class SendE2EWorkflow:
    """Workflow that tests Send API for dynamic parallelism."""

    @workflow.run
    async def run(self, items: list[int]) -> dict:
        app = lg_compile("e2e_send")
        return await app.ainvoke({"items": items})


@workflow.defn
class SubgraphE2EWorkflow:
    """Workflow that tests subgraph execution."""

    @workflow.run
    async def run(self, value: int) -> dict:
        app = lg_compile("e2e_subgraph")
        return await app.ainvoke({"value": value})


@workflow.defn
class CommandE2EWorkflow:
    """Workflow that tests Command goto API."""

    @workflow.run
    async def run(self, value: int) -> dict:
        app = lg_compile("e2e_command")
        return await app.ainvoke({"value": value})


# ==============================================================================
# Agentic Workflows
# ==============================================================================


@workflow.defn
class ReactAgentE2EWorkflow:
    """Workflow that runs a react agent with temporal tools."""

    @workflow.run
    async def run(self, question: str) -> dict[str, Any]:
        """Run the react agent and return the result."""
        with workflow.unsafe.imports_passed_through():
            from langchain_core.messages import HumanMessage

        app = lg_compile("e2e_react_agent")

        # Run the agent
        result = await app.ainvoke({"messages": [HumanMessage(content=question)]})

        # Extract the final message content
        messages = result.get("messages", [])
        if messages:
            final_message = messages[-1]
            return {
                "answer": final_message.content,
                "message_count": len(messages),
            }
        return {"answer": "", "message_count": 0}


# ==============================================================================
# Native Agent Workflows (no wrappers)
# ==============================================================================


@workflow.defn
class NativeReactAgentE2EWorkflow:
    """Workflow that runs a native react agent WITHOUT temporal wrappers.

    This tests that the Temporal integration works with plain LangGraph
    agents - no temporal_tool or temporal_model wrappers needed.
    """

    @workflow.run
    async def run(self, question: str) -> dict[str, Any]:
        """Run the native react agent and return the result."""
        with workflow.unsafe.imports_passed_through():
            from langchain_core.messages import HumanMessage

        app = lg_compile("e2e_native_react_agent")

        # Run the agent
        result = await app.ainvoke({"messages": [HumanMessage(content=question)]})

        # Extract the final message content
        messages = result.get("messages", [])
        if messages:
            final_message = messages[-1]
            return {
                "answer": final_message.content,
                "message_count": len(messages),
            }
        return {"answer": "", "message_count": 0}


# ==============================================================================
# Continue-as-New Workflows
# ==============================================================================


@workflow.defn
class ContinueAsNewE2EWorkflow:
    """Workflow demonstrating continue-as-new with checkpoint.

    This workflow demonstrates the checkpoint pattern for long-running workflows:
    1. Runs graph with should_continue callback
    2. After N ticks, should_continue returns False
    3. Workflow gets checkpoint and calls continue-as-new
    4. New execution restores from checkpoint and continues
    """

    def __init__(self) -> None:
        self._cycle_count: int = 0

    @workflow.query
    def get_cycle_count(self) -> int:
        """Query to get current cycle count."""
        return self._cycle_count

    @workflow.run
    async def run(self, input_data: ContinueAsNewInput) -> dict:
        # Restore cycle count from input
        self._cycle_count = input_data.cycle_count

        # Compile graph with checkpoint if provided (from previous continue-as-new)
        app = lg_compile("e2e_continue_as_new", checkpoint=input_data.checkpoint)

        # Define should_continue to stop after 2 ticks
        def should_continue() -> bool:
            self._cycle_count += 1
            return self._cycle_count < 2

        # Run graph with should_continue callback
        result = await app.ainvoke(
            {"value": input_data.input_value},
            should_continue=should_continue,
        )

        # Check if we stopped due to should_continue returning False
        if "__checkpoint__" in result:
            # Get checkpoint and continue-as-new
            checkpoint = result["__checkpoint__"]
            workflow.continue_as_new(
                ContinueAsNewInput(
                    input_value=input_data.input_value,
                    checkpoint=checkpoint.model_dump(),
                    cycle_count=self._cycle_count,
                )
            )

        return result


# ==============================================================================
# Subgraph Test Workflows
# ==============================================================================


@workflow.defn
class AgentSubgraphE2EWorkflow:
    """Workflow that tests create_agent as subgraph followed by another node."""

    @workflow.run
    async def run(self, query: str) -> dict:
        app = lg_compile("e2e_agent_subgraph")
        return await app.ainvoke({"messages": [{"role": "human", "content": query}]})


@workflow.defn
class SubgraphConditionalE2EWorkflow:
    """Workflow that tests subgraph followed by conditional edge."""

    @workflow.run
    async def run(self, value: int) -> dict:
        app = lg_compile("e2e_subgraph_conditional")
        return await app.ainvoke({"value": value})
