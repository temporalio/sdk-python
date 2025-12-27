"""Unit tests for LangGraph activities.

Tests for node execution activities.
These tests mock activity context and don't require a running Temporal server.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph


class TestNodeExecutionActivity:
    """Tests for the node execution activity."""

    def test_activity_captures_writes_via_config_key_send(self) -> None:
        """Activity should capture writes via CONFIG_KEY_SEND callback."""
        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._activities import langgraph_node
        from temporalio.contrib.langgraph._models import NodeActivityInput

        class State(TypedDict, total=False):
            value: int
            output: str

        def increment_node(state: State) -> State:
            return {"value": state.get("value", 0) + 10, "output": "incremented"}

        def build():
            graph = StateGraph(State)
            graph.add_node("increment", increment_node)
            graph.add_edge(START, "increment")
            graph.add_edge("increment", END)
            return graph.compile()

        LangGraphPlugin(graphs={"activity_test": build})

        # Create input
        input_data = NodeActivityInput(
            node_name="increment",
            task_id="test_task_1",
            graph_id="activity_test",
            input_state={"value": 5},
            config={},
            path=(),
            triggers=[],
        )

        # Execute activity (mock activity context)
        with patch("temporalio.activity.heartbeat"):
            result = asyncio.get_event_loop().run_until_complete(
                langgraph_node(input_data)
            )

        # Verify writes were captured
        assert len(result.writes) == 2
        write_dict = {w.channel: w.value for w in result.writes}
        assert write_dict["value"] == 15  # 5 + 10
        assert write_dict["output"] == "incremented"

    def test_activity_handles_langchain_messages(self) -> None:
        """Activity should preserve LangChain message types."""
        from langchain_core.messages import AIMessage, HumanMessage

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._activities import langgraph_node
        from temporalio.contrib.langgraph._models import NodeActivityInput

        class State(TypedDict, total=False):
            messages: list

        def agent_node(state: State) -> State:
            return {"messages": [AIMessage(content="Hello from agent!")]}

        def build():
            graph = StateGraph(State)
            graph.add_node("agent", agent_node)
            graph.add_edge(START, "agent")
            graph.add_edge("agent", END)
            return graph.compile()

        LangGraphPlugin(graphs={"message_test": build})

        input_data = NodeActivityInput(
            node_name="agent",
            task_id="test_task_2",
            graph_id="message_test",
            input_state={"messages": [HumanMessage(content="Hi")]},
            config={},
            path=(),
            triggers=[],
        )

        with patch("temporalio.activity.heartbeat"):
            result = asyncio.get_event_loop().run_until_complete(
                langgraph_node(input_data)
            )

        # Verify message type was detected
        assert len(result.writes) == 1
        write = result.writes[0]
        assert write.channel == "messages"
        assert write.value_type == "message_list"

    def test_activity_raises_for_missing_node(self) -> None:
        """Activity should raise ApplicationError for missing node."""
        from temporalio.contrib.langgraph import LangGraphPlugin, NODE_NOT_FOUND_ERROR
        from temporalio.contrib.langgraph._activities import langgraph_node
        from temporalio.contrib.langgraph._models import NodeActivityInput
        from temporalio.exceptions import ApplicationError

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node("real_node", lambda state: {"value": 1})
            graph.add_edge(START, "real_node")
            graph.add_edge("real_node", END)
            return graph.compile()

        LangGraphPlugin(graphs={"missing_node_test": build})

        input_data = NodeActivityInput(
            node_name="nonexistent_node",
            task_id="test_task_3",
            graph_id="missing_node_test",
            input_state={},
            config={},
            path=(),
            triggers=[],
        )

        with patch("temporalio.activity.heartbeat"):
            with pytest.raises(ApplicationError) as exc_info:
                asyncio.get_event_loop().run_until_complete(langgraph_node(input_data))
            assert exc_info.value.type == NODE_NOT_FOUND_ERROR
            assert "nonexistent_node" in str(exc_info.value)


