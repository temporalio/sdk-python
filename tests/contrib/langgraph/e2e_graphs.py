"""Graph builders for LangGraph E2E tests.

All graph builders used in E2E tests are defined here to ensure consistency
and avoid duplication across test files.

Naming conventions:
- Graph builder functions: build_<feature>_graph()
- Graph IDs when registered: e2e_<feature>
- State types: <Feature>State
"""

from __future__ import annotations

import operator
from datetime import timedelta
from typing import Annotated, Any

from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send


# ==============================================================================
# Simple Graph (no interrupts)
# ==============================================================================


class SimpleState(TypedDict, total=False):
    """State for simple workflow without interrupts."""

    value: int
    result: int


def _double_node(state: SimpleState) -> SimpleState:
    """Simple node that doubles the value."""
    return {"result": state.get("value", 0) * 2}


def build_simple_graph():
    """Build a simple graph without interrupts."""
    graph = StateGraph(SimpleState)
    graph.add_node("double", _double_node)
    graph.add_edge(START, "double")
    graph.add_edge("double", END)
    return graph.compile()


# ==============================================================================
# Approval Graph (single interrupt)
# ==============================================================================


class ApprovalState(TypedDict, total=False):
    """State for approval workflow."""

    value: int
    approved: bool
    approval_reason: str


def _approval_node(state: ApprovalState) -> ApprovalState:
    """Node that requests approval via interrupt."""
    from langgraph.types import interrupt

    approval_response = interrupt(
        {
            "question": "Do you approve this value?",
            "current_value": state.get("value", 0),
        }
    )

    return {
        "approved": approval_response.get("approved", False),
        "approval_reason": approval_response.get("reason", ""),
    }


def _process_node(state: ApprovalState) -> ApprovalState:
    """Node that processes the approved value."""
    if state.get("approved"):
        return {"value": state.get("value", 0) * 2}
    return {"value": 0}


def build_approval_graph():
    """Build the approval graph with interrupt."""
    graph = StateGraph(ApprovalState)
    graph.add_node("request_approval", _approval_node)
    graph.add_node("process", _process_node)
    graph.add_edge(START, "request_approval")
    graph.add_edge("request_approval", "process")
    graph.add_edge("process", END)
    return graph.compile()


# ==============================================================================
# Multi-Interrupt Graph (sequential interrupts)
# ==============================================================================


class MultiInterruptState(TypedDict, total=False):
    """State for multi-interrupt workflow."""

    value: int
    step1_result: str
    step2_result: str


def _step1_node(state: MultiInterruptState) -> MultiInterruptState:
    """First step that requires human input."""
    from langgraph.types import interrupt

    response = interrupt({"step": 1, "question": "Enter value for step 1"})
    return {"step1_result": str(response)}


def _step2_node(state: MultiInterruptState) -> MultiInterruptState:
    """Second step that requires human input."""
    from langgraph.types import interrupt

    response = interrupt({"step": 2, "question": "Enter value for step 2"})
    return {"step2_result": str(response)}


def build_multi_interrupt_graph():
    """Build a graph with multiple sequential interrupts."""
    graph = StateGraph(MultiInterruptState)
    graph.add_node("step1", _step1_node)
    graph.add_node("step2", _step2_node)
    graph.add_edge(START, "step1")
    graph.add_edge("step1", "step2")
    graph.add_edge("step2", END)
    return graph.compile()


# ==============================================================================
# Store Graph (cross-node persistence)
# ==============================================================================


class StoreState(TypedDict, total=False):
    """State for store test workflow."""

    user_id: str
    node1_read: str | None
    node2_read: str | None


def _store_node1(state: StoreState) -> StoreState:
    """Node that writes to store and reads from it."""
    from langgraph.config import get_store

    store = get_store()
    user_id = state.get("user_id", "default")

    # Try to read existing value (should be None on first run)
    existing = store.get(("user", user_id), "preferences")
    existing_value = existing.value["theme"] if existing else None

    # Write a new value to the store
    store.put(
        ("user", user_id), "preferences", {"theme": "dark", "written_by": "node1"}
    )

    return {"node1_read": existing_value}


def _store_node2(state: StoreState) -> StoreState:
    """Node that reads from store (should see node1's write)."""
    from langgraph.config import get_store

    store = get_store()
    user_id = state.get("user_id", "default")

    # Read the value written by node1
    item = store.get(("user", user_id), "preferences")
    read_value = item.value["theme"] if item else None

    return {"node2_read": read_value}


def build_store_graph():
    """Build a graph that uses store for cross-node persistence."""
    graph = StateGraph(StoreState)
    graph.add_node("node1", _store_node1)
    graph.add_node("node2", _store_node2)
    graph.add_edge(START, "node1")
    graph.add_edge("node1", "node2")
    graph.add_edge("node2", END)
    return graph.compile()


# ==============================================================================
# Counter Graph (cross-invocation persistence)
# ==============================================================================


class CounterState(TypedDict, total=False):
    """State for multi-invocation store test workflow."""

    user_id: str
    invocation_num: int
    previous_count: int | None
    current_count: int | None


def _counter_node(state: CounterState) -> CounterState:
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


def build_counter_graph():
    """Build a graph that increments a counter in the store.

    Used to test store persistence across multiple graph invocations.
    """
    graph = StateGraph(CounterState)
    graph.add_node("counter", _counter_node)
    graph.add_edge(START, "counter")
    graph.add_edge("counter", END)
    return graph.compile()


# ==============================================================================
# Send API Graph (dynamic parallelism)
# ==============================================================================


class SendState(TypedDict, total=False):
    """State for Send API test."""

    items: list[int]
    results: Annotated[list[int], operator.add]


def _send_setup_node(state: SendState) -> SendState:
    """Setup node that just passes through."""
    return {}


def _send_continue_to_workers(state: SendState) -> list[Send]:
    """Conditional edge function that creates parallel worker tasks via Send."""
    items = state.get("items", [])
    # Return a list of Send objects to create parallel tasks
    return [Send("worker", {"item": item}) for item in items]


def _send_worker_node(state: Any) -> dict[str, Any]:
    """Worker node that processes a single item.

    Note: When using Send API, worker receives a dict with the Send payload,
    not the full graph state. Type is Any to accommodate this.
    """
    item = state.get("item", 0)
    # Double the item
    return {"results": [item * 2]}


def build_send_graph():
    """Build a graph that uses Send for dynamic parallelism."""
    graph = StateGraph(SendState)
    graph.add_node("setup", _send_setup_node)
    graph.add_node("worker", _send_worker_node)
    graph.add_edge(START, "setup")
    # Send API: conditional edge function returns list of Send objects
    graph.add_conditional_edges("setup", _send_continue_to_workers, ["worker"])
    graph.add_edge("worker", END)
    return graph.compile()


# ==============================================================================
# Subgraph (nested graphs)
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


def _parent_start_node(state: ParentState) -> ParentState:
    """Parent node that prepares state for child."""
    return {"value": state.get("value", 0) + 10}


def _child_process_node(state: ChildState) -> ChildState:
    """Child node that processes the value."""
    return {"child_result": state.get("value", 0) * 3}


def _parent_end_node(state: ParentState) -> ParentState:
    """Parent node that finalizes result."""
    return {"final_result": state.get("child_result", 0) + 100}


def build_subgraph():
    """Build a parent graph with a child subgraph."""
    # Create child subgraph
    child = StateGraph(ChildState)
    child.add_node("child_process", _child_process_node)
    child.add_edge(START, "child_process")
    child.add_edge("child_process", END)
    child_compiled = child.compile()

    # Create parent graph with child as a node
    parent = StateGraph(ParentState)
    parent.add_node("parent_start", _parent_start_node)
    parent.add_node("child_graph", child_compiled)
    parent.add_node("parent_end", _parent_end_node)
    parent.add_edge(START, "parent_start")
    parent.add_edge("parent_start", "child_graph")
    parent.add_edge("child_graph", "parent_end")
    parent.add_edge("parent_end", END)
    return parent.compile()


# ==============================================================================
# Command Graph (goto navigation)
# ==============================================================================


class CommandState(TypedDict, total=False):
    """State for Command goto test."""

    value: int
    path: Annotated[list[str], operator.add]  # Reducer to accumulate path entries
    result: int


def _command_start_node(state: CommandState) -> Command:
    """Node that uses Command to navigate."""
    value = state.get("value", 0)

    # Use Command to update state AND goto specific node
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


def _command_middle_node(state: CommandState) -> CommandState:
    """Middle node in the path."""
    return {"path": ["middle"], "value": state.get("value", 0) * 2}


def _command_finish_node(state: CommandState) -> CommandState:
    """Final node that computes result."""
    return {"path": ["finish"], "result": state.get("value", 0) + 1000}


def build_command_graph():
    """Build a graph that uses Command for navigation.

    With Command, we don't add a static edge from 'start' - the Command(goto=...)
    determines where to go next.
    """
    graph = StateGraph(CommandState)
    graph.add_node("start", _command_start_node)
    graph.add_node("middle", _command_middle_node)
    graph.add_node("finish", _command_finish_node)
    graph.add_edge(START, "start")
    # NO edge from start - Command(goto=...) handles the routing
    graph.add_edge("middle", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


# ==============================================================================
# React Agent Graph (tool calling)
# ==============================================================================


def build_react_agent_graph():
    """Build a react agent graph for E2E testing.

    Note: For production use, prefer `from langchain.agents import create_agent`
    as langgraph.prebuilt.create_react_agent is deprecated. We use the deprecated
    version here to minimize test dependencies (langchain-core only).
    """
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent

    # Create a proper fake model that inherits from BaseChatModel
    class FakeToolCallingModel(BaseChatModel):
        """Fake model that simulates tool calling for testing."""

        @property
        def _llm_type(self) -> str:
            return "fake-tool-model"

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> ChatResult:
            """Generate a response, simulating tool calling."""
            # Check if we have a tool result in messages
            has_tool_result = any(isinstance(m, ToolMessage) for m in messages)

            if not has_tool_result:
                # First call - return a tool call
                ai_message = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_123",
                            "name": "calculator",
                            "args": {"expression": "2 + 2"},
                        }
                    ],
                )
            else:
                # After tool result - return final answer
                ai_message = AIMessage(
                    content="The calculation result is 4.",
                )

            return ChatResult(
                generations=[ChatGeneration(message=ai_message)],
                llm_output={"model": "fake-tool-model"},
            )

        def bind_tools(
            self,
            tools: Any,
            **kwargs: Any,
        ) -> "FakeToolCallingModel":
            """Return self - tools are handled in _generate."""
            return self

    # Create tools - plain tools, no wrapper needed
    @tool
    def calculator(expression: str) -> str:
        """Calculate a math expression. Input should be a valid Python math expression."""
        try:
            result = eval(expression)
            return f"Result: {result}"
        except Exception as e:
            return f"Error: {e}"

    # Create fake model
    model = FakeToolCallingModel()

    # Create react agent with plain tools
    agent = create_react_agent(model, [calculator])

    return agent


# ==============================================================================
# Native Agent Graph (no wrappers - tests simplification)
# ==============================================================================


def build_native_react_agent_graph():
    """Build an agent using ONLY native LangGraph - no temporal wrappers.

    This tests that the Temporal integration works without temporal_tool or
    temporal_model wrappers. The model and tools execute directly within
    the node activities.

    Note: For production use, prefer `from langchain.agents import create_agent`
    as langgraph.prebuilt.create_react_agent is deprecated. We use the deprecated
    version here to minimize test dependencies (langchain-core only).
    """
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent

    class FakeToolCallingModel(BaseChatModel):
        """Fake model that simulates a multi-step tool calling conversation.

        Step 1: Call get_weather tool
        Step 2: Call get_temperature tool (after seeing weather result)
        Step 3: Return final answer (after seeing both results)

        This ensures the agent loops at least twice through the tools node.
        """

        @property
        def _llm_type(self) -> str:
            return "fake-multi-step-model"

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> ChatResult:
            """Generate response based on conversation state."""
            # Count tool results to determine which step we're at
            tool_results = [m for m in messages if isinstance(m, ToolMessage)]
            num_tool_results = len(tool_results)

            if num_tool_results == 0:
                # Step 1: Call get_weather
                ai_message = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_weather",
                            "name": "get_weather",
                            "args": {"city": "San Francisco"},
                        }
                    ],
                )
            elif num_tool_results == 1:
                # Step 2: Call get_temperature (after seeing weather)
                ai_message = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_temp",
                            "name": "get_temperature",
                            "args": {"city": "San Francisco"},
                        }
                    ],
                )
            else:
                # Step 3: Final answer after seeing both results
                ai_message = AIMessage(
                    content="Based on my research: San Francisco is sunny with 72°F temperature.",
                )

            return ChatResult(
                generations=[ChatGeneration(message=ai_message)],
                llm_output={"model": "fake-multi-step-model"},
            )

        def bind_tools(
            self,
            tools: Any,
            **kwargs: Any,
        ) -> "FakeToolCallingModel":
            """Return self - tools are handled in _generate."""
            return self

    # Create plain tools - NO temporal_tool wrapper
    @tool
    def get_weather(city: str) -> str:
        """Get the weather for a city."""
        return f"Weather in {city}: Sunny"

    @tool
    def get_temperature(city: str) -> str:
        """Get the temperature for a city."""
        return f"Temperature in {city}: 72°F"

    # Create model - NO temporal_model wrapper
    model = FakeToolCallingModel()

    # Create react agent using native LangGraph
    agent = create_react_agent(model, [get_weather, get_temperature])

    return agent


# ==============================================================================
# Continue-as-New Graph (checkpoint/restore)
# ==============================================================================


class ContinueAsNewState(TypedDict, total=False):
    """State for continue-as-new test workflow."""

    value: int
    step: int


def _continue_increment_node(state: ContinueAsNewState) -> ContinueAsNewState:
    """Node that increments the step counter."""
    return {
        "step": state.get("step", 0) + 1,
        "value": state.get("value", 0) + 10,
    }


def build_continue_as_new_graph():
    """Build a graph for testing continue-as-new with checkpoints."""
    graph = StateGraph(ContinueAsNewState)
    graph.add_node("increment", _continue_increment_node)
    graph.add_edge(START, "increment")
    graph.add_edge("increment", END)
    return graph.compile()
