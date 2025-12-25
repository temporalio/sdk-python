"""Example: Customer Support Agent with Temporal + LangGraph.

This example demonstrates a non-trivial LangGraph graph running with Temporal:
- Multi-node graph with conditional routing
- Per-node configuration (timeouts, retry policies, task queues)
- LangChain message handling
- Integration with Temporal workflows

To run this example:
    1. Start a Temporal server (e.g., `temporal server start-dev`)
    2. Run this file: `python -m temporalio.contrib.langgraph.example`

Graph Structure:
    START -> classify -> route_by_category
                          |
              +-----------+-----------+
              |           |           |
              v           v           v
          billing    technical    general
              |           |           |
              +-----------+-----------+
                          |
                          v
                    should_escalate
                          |
                    +-----+-----+
                    |           |
                    v           v
                escalate    respond
                    |           |
                    +-----------+
                          |
                          v
                         END
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from typing_extensions import TypedDict

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from temporalio.contrib.langgraph import LangGraphPlugin, compile

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


# =============================================================================
# State Definition
# =============================================================================


class SupportState(TypedDict, total=False):
    """State for the customer support agent."""

    messages: list[BaseMessage]
    category: str  # "billing", "technical", "general"
    sentiment: str  # "positive", "neutral", "negative"
    should_escalate: bool
    response: str
    escalation_reason: str | None


# =============================================================================
# Node Functions
# =============================================================================


def classify_query(state: SupportState) -> SupportState:
    """Classify the customer query into a category.

    In production, this would call an LLM to classify.
    """
    messages = state.get("messages", [])
    if not messages:
        return {"category": "general", "sentiment": "neutral"}

    # Simple keyword-based classification for demo
    # Handle both string and list content types
    content = messages[-1].content if messages else ""
    last_message = content.lower() if isinstance(content, str) else str(content).lower()

    if any(word in last_message for word in ["bill", "charge", "payment", "invoice"]):
        category = "billing"
    elif any(word in last_message for word in ["error", "bug", "broken", "not working", "crash"]):
        category = "technical"
    else:
        category = "general"

    # Simple sentiment detection
    if any(word in last_message for word in ["angry", "frustrated", "terrible", "awful"]):
        sentiment = "negative"
    elif any(word in last_message for word in ["thanks", "great", "love", "excellent"]):
        sentiment = "positive"
    else:
        sentiment = "neutral"

    return {"category": category, "sentiment": sentiment}


def handle_billing(state: SupportState) -> SupportState:
    """Handle billing-related queries."""
    return {
        "response": "I understand you have a billing question. "
        "Let me look up your account details and help resolve this.",
        "should_escalate": state.get("sentiment") == "negative",
    }


def handle_technical(state: SupportState) -> SupportState:
    """Handle technical support queries."""
    return {
        "response": "I see you're experiencing a technical issue. "
        "Let me help troubleshoot this problem.",
        "should_escalate": state.get("sentiment") == "negative",
    }


def handle_general(state: SupportState) -> SupportState:
    """Handle general queries."""
    return {
        "response": "Thank you for reaching out! How can I assist you today?",
        "should_escalate": False,
    }


def escalate_to_human(state: SupportState) -> SupportState:
    """Escalate the conversation to a human agent."""
    return {
        "escalation_reason": f"Customer sentiment: {state.get('sentiment')}",
        "messages": state.get("messages", [])
        + [AIMessage(content="I'm connecting you with a human agent who can better assist you.")],
    }


def generate_response(state: SupportState) -> SupportState:
    """Generate the final response."""
    response = state.get("response", "How can I help you?")
    return {
        "messages": state.get("messages", []) + [AIMessage(content=response)],
    }


# =============================================================================
# Routing Functions
# =============================================================================


def route_by_category(state: SupportState) -> Literal["billing", "technical", "general"]:
    """Route to the appropriate handler based on category."""
    return state.get("category", "general")  # type: ignore[return-value]


def should_escalate(state: SupportState) -> Literal["escalate", "respond"]:
    """Decide whether to escalate or respond directly."""
    if state.get("should_escalate"):
        return "escalate"
    return "respond"


# =============================================================================
# Graph Builder
# =============================================================================


def build_support_agent() -> Any:
    """Build the customer support agent graph.

    This demonstrates:
    - Multiple nodes with different responsibilities
    - Conditional routing based on state
    - Per-node Temporal configuration via metadata
    - LangGraph RetryPolicy mapped to Temporal RetryPolicy
    """
    graph = StateGraph(SupportState)

    # Add nodes with Temporal-specific configuration
    # Note: For this example, we don't specify task_queue so activities run on
    # the workflow's task queue. In production, you could route different nodes
    # to specialized workers (e.g., GPU workers for LLM inference).
    graph.add_node(
        "classify",
        classify_query,
        metadata={
            "temporal": {
                "activity_timeout": timedelta(seconds=30),
            }
        },
        # Retry quickly for classification
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=0.5),
    )

    graph.add_node(
        "billing",
        handle_billing,
        metadata={
            "temporal": {
                "activity_timeout": timedelta(minutes=2),
            }
        },
        # Billing lookups may need more retries
        retry_policy=RetryPolicy(max_attempts=5, initial_interval=1.0, backoff_factor=2.0),
    )

    graph.add_node(
        "technical",
        handle_technical,
        metadata={
            "temporal": {
                "activity_timeout": timedelta(minutes=5),
                "heartbeat_timeout": timedelta(seconds=30),
            }
        },
        # Technical operations may be slower
        retry_policy=RetryPolicy(max_attempts=3, initial_interval=2.0),
    )

    graph.add_node(
        "general",
        handle_general,
        metadata={
            "temporal": {
                "activity_timeout": timedelta(seconds=30),
            }
        },
    )

    graph.add_node(
        "escalate",
        escalate_to_human,
        metadata={
            "temporal": {
                "activity_timeout": timedelta(seconds=10),
            }
        },
    )

    graph.add_node(
        "respond",
        generate_response,
        metadata={
            "temporal": {
                "activity_timeout": timedelta(seconds=10),
            }
        },
    )

    # Define edges
    graph.add_edge(START, "classify")

    # Conditional routing based on category
    graph.add_conditional_edges(
        "classify",
        route_by_category,
        {
            "billing": "billing",
            "technical": "technical",
            "general": "general",
        },
    )

    # All handlers route to escalation check
    graph.add_conditional_edges(
        "billing",
        should_escalate,
        {"escalate": "escalate", "respond": "respond"},
    )
    graph.add_conditional_edges(
        "technical",
        should_escalate,
        {"escalate": "escalate", "respond": "respond"},
    )
    graph.add_edge("general", "respond")

    # Final edges to END
    graph.add_edge("escalate", END)
    graph.add_edge("respond", END)

    return graph.compile()


# =============================================================================
# Temporal Workflow
# =============================================================================


@workflow.defn
class CustomerSupportWorkflow:
    """Temporal workflow that executes the customer support agent.

    This workflow:
    - Uses compile() to get a TemporalLangGraphRunner
    - Executes the graph with full Temporal durability
    - Each node runs as a separate activity with its own config
    """

    @workflow.run
    async def run(self, customer_query: str) -> dict:
        """Run the customer support agent.

        Args:
            customer_query: The customer's question or issue.

        Returns:
            The final state including the response.
        """
        # Get the compiled graph runner
        app = compile(
            "support_agent",
            default_activity_timeout=timedelta(minutes=1),
            default_max_retries=3,
        )

        # Create initial state with the customer message
        initial_state: dict[str, Any] = {
            "messages": [HumanMessage(content=customer_query)],
        }

        # Execute the graph - each node becomes a Temporal activity
        final_state = await app.ainvoke(initial_state)

        return final_state


# =============================================================================
# Main - Run the Example
# =============================================================================


async def main():
    """Run the example."""
    import uuid

    # Create the plugin with our graph
    plugin = LangGraphPlugin(
        graphs={"support_agent": build_support_agent},
        default_activity_timeout=timedelta(minutes=5),
    )

    # Connect to Temporal with the plugin
    client = await Client.connect("localhost:7233", plugins=[plugin])

    # Generate unique run ID for this execution
    run_id = uuid.uuid4().hex[:8]

    # Create worker
    # Note: In production, you'd have separate workers for different task queues
    # Note: We disable the workflow sandbox because LangGraph/LangChain imports
    # contain non-deterministic code. The actual graph execution happens in
    # activities which run outside the sandbox.
    task_queue = f"langgraph-support-{run_id}"  # Fresh queue per run
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[CustomerSupportWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
        # Activities are auto-registered by the plugin
    ):
        print("Worker started. Running example queries...\n")

        # Example 1: Billing query
        print("=" * 60)
        print("Example 1: Billing Query")
        print("=" * 60)
        result = await client.execute_workflow(
            CustomerSupportWorkflow.run,
            "I was charged twice for my subscription last month!",
            id=f"support-billing-{run_id}",
            task_queue=task_queue,
        )
        print(f"Category: {result.get('category')}")
        print(f"Sentiment: {result.get('sentiment')}")
        print(f"Escalated: {result.get('should_escalate')}")
        if result.get("messages"):
            last_msg = result['messages'][-1]
            # Handle both message objects and dicts
            content = last_msg.content if hasattr(last_msg, 'content') else last_msg.get('content')
            print(f"Response: {content}")
        print()

        # Example 2: Technical query
        print("=" * 60)
        print("Example 2: Technical Query (Frustrated)")
        print("=" * 60)
        result = await client.execute_workflow(
            CustomerSupportWorkflow.run,
            "This is terrible! The app keeps crashing and I'm so frustrated!",
            id=f"support-technical-{run_id}",
            task_queue=task_queue,
        )
        print(f"Category: {result.get('category')}")
        print(f"Sentiment: {result.get('sentiment')}")
        print(f"Escalated: {result.get('should_escalate')}")
        print(f"Escalation Reason: {result.get('escalation_reason')}")
        if result.get("messages"):
            last_msg = result['messages'][-1]
            content = last_msg.content if hasattr(last_msg, 'content') else last_msg.get('content')
            print(f"Response: {content}")
        print()

        # Example 3: General query
        print("=" * 60)
        print("Example 3: General Query")
        print("=" * 60)
        result = await client.execute_workflow(
            CustomerSupportWorkflow.run,
            "Hi! I'd like to learn more about your product.",
            id=f"support-general-{run_id}",
            task_queue=task_queue,
        )
        print(f"Category: {result.get('category')}")
        if result.get("messages"):
            last_msg = result['messages'][-1]
            content = last_msg.content if hasattr(last_msg, 'content') else last_msg.get('content')
            print(f"Response: {content}")
        else:
            print("Response: N/A")
        print()

        print("Example complete!")


if __name__ == "__main__":
    asyncio.run(main())
