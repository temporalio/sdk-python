"""Workflow definitions for LangGraph e2e tests.

These workflows are defined in a separate module to ensure proper sandbox
compatibility. LangGraph imports are wrapped with imports_passed_through().
"""

from dataclasses import dataclass
from typing import Any

from temporalio import workflow

# Use imports_passed_through for langgraph types used in workflows
with workflow.unsafe.imports_passed_through():
    from langgraph.types import Command

    from temporalio.contrib.langgraph import compile as lg_compile


@dataclass
class ContinueAsNewInput:
    """Input for ContinueAsNewWorkflow."""

    input_value: int
    checkpoint: dict | None = None
    cycle_count: int = 0  # Track how many cycles we've completed


@workflow.defn
class SimpleGraphWorkflow:
    """Simple workflow that runs a graph without interrupts."""

    @workflow.run
    async def run(self, input_value: int) -> dict:
        app = lg_compile("e2e_simple")
        return await app.ainvoke({"value": input_value})


@workflow.defn
class ApprovalWorkflow:
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
            await workflow.wait_condition(
                lambda: self._approval_response is not None
            )

            # Resume with the approval response
            result = await app.ainvoke(Command(resume=self._approval_response))

        return result


@workflow.defn
class RejectionWorkflow:
    """Workflow for testing interrupt rejection."""

    def __init__(self) -> None:
        self._approval_response: dict | None = None

    @workflow.signal
    def provide_approval(self, response: dict) -> None:
        """Signal to provide approval response."""
        self._approval_response = response

    @workflow.run
    async def run(self, input_value: int) -> dict:
        app = lg_compile("e2e_approval_reject")

        result = await app.ainvoke({"value": input_value})

        if "__interrupt__" in result:
            await workflow.wait_condition(
                lambda: self._approval_response is not None
            )
            result = await app.ainvoke(Command(resume=self._approval_response))

        return result


@workflow.defn
class MultiInterruptWorkflow:
    """Workflow that handles multiple interrupts in sequence."""

    def __init__(self) -> None:
        self._response: Any = None
        self._interrupt_count: int = 0
        self._current_interrupt: Any = None
        self._invocation_id: int = 0

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
        if not hasattr(self, '_app'):
            return {"error": "no app"}
        return {
            "has_interrupted_state": self._app._interrupted_state is not None,
            "interrupted_state": self._app._interrupted_state,
            "interrupted_node": self._app._interrupted_node_name,
            "completed_nodes": list(self._app._completed_nodes_in_cycle),
            "resume_value": self._app._resume_value,
            "resume_used": self._app._resume_used,
            "pending_interrupt": self._app._pending_interrupt,
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


@workflow.defn
class ContinueAsNewWorkflow:
    """Workflow demonstrating continue-as-new with checkpoint.

    This workflow demonstrates the checkpoint pattern for long-running workflows:
    1. Runs graph with should_continue callback
    2. After 2 ticks, should_continue returns False
    3. Workflow gets checkpoint and calls continue-as-new
    4. New execution restores from checkpoint and continues

    The should_continue callback is called once per graph tick (BSP superstep).
    Each tick processes one layer of nodes in the graph. By tracking ticks,
    we can limit execution and checkpoint before Temporal's history grows too large.

    This simulates a long-running agent that needs to continue-as-new
    due to history size limits.
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
        # This is called after each tick, so we increment and check
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
