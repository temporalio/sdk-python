"""Unit tests for LangGraph activities.

Tests for execute_node, execute_tool, and execute_chat_model activities.
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
        from temporalio.contrib.langgraph._activities import execute_node
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
                execute_node(input_data)
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
        from temporalio.contrib.langgraph._activities import execute_node
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
                execute_node(input_data)
            )

        # Verify message type was detected
        assert len(result.writes) == 1
        write = result.writes[0]
        assert write.channel == "messages"
        assert write.value_type == "message_list"

    def test_activity_raises_for_missing_node(self) -> None:
        """Activity should raise ValueError for missing node."""
        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._activities import execute_node
        from temporalio.contrib.langgraph._models import NodeActivityInput

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
            with pytest.raises(ValueError, match="not found"):
                asyncio.get_event_loop().run_until_complete(execute_node(input_data))


class TestToolActivity:
    """Tests for the tool execution activity."""

    def test_tool_activity_executes_registered_tool(self) -> None:
        """Tool activity should execute registered tools."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph._activities import execute_tool
        from temporalio.contrib.langgraph._models import ToolActivityInput
        from temporalio.contrib.langgraph._tool_registry import register_tool

        @tool
        def add_numbers(a: int, b: int) -> int:
            """Add two numbers together."""
            return a + b

        register_tool(add_numbers)

        input_data = ToolActivityInput(
            tool_name="add_numbers",
            tool_input={"a": 5, "b": 3},
        )

        result = asyncio.get_event_loop().run_until_complete(execute_tool(input_data))

        assert result.output == 8

    def test_tool_activity_raises_for_missing_tool(self) -> None:
        """Tool activity should raise KeyError for unregistered tools."""
        from temporalio.contrib.langgraph._activities import execute_tool
        from temporalio.contrib.langgraph._models import ToolActivityInput

        input_data = ToolActivityInput(
            tool_name="nonexistent_tool",
            tool_input={},
        )

        with pytest.raises(KeyError, match="not found"):
            asyncio.get_event_loop().run_until_complete(execute_tool(input_data))


class TestChatModelActivity:
    """Tests for the chat model execution activity."""

    def test_model_activity_executes_registered_model(self) -> None:
        """Model activity should execute registered models."""
        from unittest.mock import AsyncMock, MagicMock

        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        from temporalio.contrib.langgraph._activities import execute_chat_model
        from temporalio.contrib.langgraph._models import ChatModelActivityInput
        from temporalio.contrib.langgraph._model_registry import register_model

        # Create a mock model with proper async _agenerate
        mock_model = MagicMock()
        mock_model.model_name = "test-model-activity"

        # Create a proper ChatResult
        mock_result = ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content="Hello!"),
                    generation_info={"finish_reason": "stop"},
                )
            ],
            llm_output={"usage": {"tokens": 10}},
        )
        mock_model._agenerate = AsyncMock(return_value=mock_result)

        register_model(mock_model)

        input_data = ChatModelActivityInput(
            model_name="test-model-activity",
            messages=[{"content": "Hi", "type": "human"}],
            stop=None,
            kwargs={},
        )

        result = asyncio.get_event_loop().run_until_complete(
            execute_chat_model(input_data)
        )

        assert len(result.generations) == 1
        assert result.llm_output == {"usage": {"tokens": 10}}

    def test_model_activity_raises_for_missing_model(self) -> None:
        """Model activity should raise KeyError for unregistered models."""
        from temporalio.contrib.langgraph._activities import execute_chat_model
        from temporalio.contrib.langgraph._models import ChatModelActivityInput

        input_data = ChatModelActivityInput(
            model_name="nonexistent-model",
            messages=[{"content": "Hi", "type": "human"}],
            stop=None,
            kwargs={},
        )

        with pytest.raises(KeyError, match="not found"):
            asyncio.get_event_loop().run_until_complete(execute_chat_model(input_data))
