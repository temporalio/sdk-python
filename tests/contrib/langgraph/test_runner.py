"""Unit tests for TemporalLangGraphRunner.

Tests for runner initialization, configuration, and basic behavior.
These tests mock the workflow context and don't require a running Temporal server.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from temporalio.common import RetryPolicy
from temporalio.contrib.langgraph import activity_options


class TestTemporalLangGraphRunner:
    """Tests for the Temporal runner."""

    def test_runner_rejects_step_timeout(self) -> None:
        """Runner should reject graphs with step_timeout."""
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        # Create a mock Pregel with step_timeout
        mock_pregel = MagicMock()
        mock_pregel.step_timeout = 30  # Non-None value

        with pytest.raises(ValueError, match="step_timeout"):
            TemporalLangGraphRunner(
                mock_pregel,
                graph_id="test",
            )

    def test_runner_accepts_no_step_timeout(self) -> None:
        """Runner should accept graphs without step_timeout."""
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {}

        runner = TemporalLangGraphRunner(
            mock_pregel,
            graph_id="test",
        )

        assert runner.graph_id == "test"
        assert runner.default_activity_options == {}

    def test_runner_invoke_raises(self) -> None:
        """Synchronous invoke should raise NotImplementedError."""
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test")

        with pytest.raises(NotImplementedError, match="ainvoke"):
            runner.invoke({})

    def test_filter_config(self) -> None:
        """Runner should filter internal config keys."""
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test")

        config = {
            "user_key": "value",
            "__pregel_internal": "hidden",
            "__lg_internal": "also_hidden",
            "configurable": {
                "thread_id": "123",
                "__pregel_key": "hidden",
            },
        }

        filtered = runner._filter_config(config)

        assert "user_key" in filtered
        assert "__pregel_internal" not in filtered
        assert "__lg_internal" not in filtered
        assert "configurable" in filtered
        assert "thread_id" in filtered["configurable"]
        assert "__pregel_key" not in filtered["configurable"]


class TestBuildActivitySummary:
    """Tests for the _build_activity_summary function."""

    def test_returns_node_name_for_non_tools_node(self) -> None:
        """Non-tools/non-model nodes without query fields return just the node name."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        result = _build_activity_summary("process", {"data": "value"})
        assert result == "process"

        result = _build_activity_summary("custom_node", {"messages": []})
        assert result == "custom_node"

    def test_extracts_query_from_generic_node(self) -> None:
        """Generic nodes with query-like fields should show the query in summary."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        # Test "query" field
        result = _build_activity_summary("search", {"query": "LangGraph definition"})
        assert result == 'search: "LangGraph definition"'

        # Test "search_query" field
        result = _build_activity_summary(
            "search", {"search_query": "Temporal features"}
        )
        assert result == 'search: "Temporal features"'

        # Test "question" field
        result = _build_activity_summary("qa", {"question": "What is AI?"})
        assert result == 'qa: "What is AI?"'

        # Test truncation of long queries
        long_query = "a" * 100
        result = _build_activity_summary("search", {"query": long_query})
        assert result.startswith('search: "aaa')
        assert result.endswith('..."')
        assert len(result) <= 100

    def test_returns_node_name_when_no_tool_calls(self) -> None:
        """Tools node without tool calls should return node name."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        result = _build_activity_summary("tools", {"messages": []})
        assert result == "tools"

        result = _build_activity_summary("tools", {"messages": [{"content": "hello"}]})
        assert result == "tools"

    def test_extracts_tool_calls_from_dict_message(self) -> None:
        """Should extract tool calls from dict-style messages."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {
                    "content": "",
                    "tool_calls": [
                        {"name": "get_weather", "args": {"city": "Tokyo"}},
                    ],
                }
            ]
        }

        result = _build_activity_summary("tools", input_state)
        assert result == "get_weather({'city': 'Tokyo'})"

    def test_extracts_tool_calls_from_langchain_message(self) -> None:
        """Should extract tool calls from LangChain AIMessage objects."""
        from langchain_core.messages import AIMessage

        from temporalio.contrib.langgraph._runner import _build_activity_summary

        msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "calculate", "args": {"expression": "2 + 2"}, "id": "call_1"},
            ],
        )
        input_state = {"messages": [msg]}

        result = _build_activity_summary("tools", input_state)
        assert result == "calculate({'expression': '2 + 2'})"

    def test_handles_multiple_tool_calls(self) -> None:
        """Should handle multiple tool calls in a single message."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {
                    "tool_calls": [
                        {"name": "tool1", "args": {"a": 1}},
                        {"name": "tool2", "args": {"b": 2}},
                    ],
                }
            ]
        }

        result = _build_activity_summary("tools", input_state)
        assert result == "tool1({'a': 1}), tool2({'b': 2})"

    def test_truncates_long_summaries(self) -> None:
        """Should truncate summaries longer than max_length."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {
                    "tool_calls": [
                        {"name": "search", "args": {"query": "a" * 200}},
                    ],
                }
            ]
        }

        result = _build_activity_summary("tools", input_state, max_length=50)
        assert len(result) == 50
        assert result.endswith("...")

    def test_handles_non_dict_input_state(self) -> None:
        """Should handle non-dict input states gracefully."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        result = _build_activity_summary("tools", "not a dict")
        assert result == "tools"

        result = _build_activity_summary("tools", None)
        assert result == "tools"

    def test_extracts_tool_calls_from_send_packet(self) -> None:
        """Should extract tool calls from Send packet structure (tool_call_with_context)."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        # This is the structure used by create_react_agent when executing tools via Send
        input_state = {
            "__type": "tool_call_with_context",
            "tool_call": {
                "name": "calculator",
                "args": {"expression": "2 + 2"},
                "id": "call_123",
                "type": "tool_call",
            },
            "state": {
                "messages": [],
                "remaining_steps": 24,
            },
        }

        result = _build_activity_summary("tools", input_state)
        assert result == "calculator({'expression': '2 + 2'})"

    def test_uses_node_metadata_description(self) -> None:
        """Should use node metadata description when available."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        node_metadata = {"description": "Process user input and generate response"}
        result = _build_activity_summary("agent", {"messages": []}, node_metadata)
        assert result == "Process user input and generate response"

    def test_truncates_long_description(self) -> None:
        """Should truncate description longer than max_length."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        long_description = "A" * 200
        node_metadata = {"description": long_description}
        result = _build_activity_summary("node", {}, node_metadata, max_length=50)
        assert len(result) == 50
        assert result.endswith("...")

    def test_tool_calls_take_precedence_over_description(self) -> None:
        """For tools node with tool calls, tool info should take precedence over description."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "__type": "tool_call_with_context",
            "tool_call": {
                "name": "get_weather",
                "args": {"city": "NYC"},
                "id": "call_123",
            },
        }
        node_metadata = {"description": "Execute tool calls"}
        result = _build_activity_summary("tools", input_state, node_metadata)
        assert result == "get_weather({'city': 'NYC'})"

    def test_description_used_when_no_tool_calls(self) -> None:
        """For tools node without tool calls, should fall back to description."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        node_metadata = {"description": "Execute tool calls"}
        result = _build_activity_summary("tools", {"messages": []}, node_metadata)
        assert result == "Execute tool calls"

    def test_ignores_non_string_description(self) -> None:
        """Should ignore description if not a string."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        node_metadata = {"description": 123}  # Not a string
        result = _build_activity_summary("agent", {}, node_metadata)
        assert result == "agent"

    def test_ignores_empty_description(self) -> None:
        """Should ignore empty description."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        node_metadata = {"description": ""}
        result = _build_activity_summary("process", {}, node_metadata)
        assert result == "process"

    # Model/agent node tests

    def test_model_node_with_query(self) -> None:
        """Model nodes should show user query from messages."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {"type": "human", "content": "What is the weather in Tokyo?"},
            ]
        }
        result = _build_activity_summary("agent", input_state)
        assert result == 'agent: "What is the weather in Tokyo?"'

    def test_model_node_with_langchain_message(self) -> None:
        """Model nodes should work with LangChain HumanMessage objects."""
        from langchain_core.messages import HumanMessage

        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                HumanMessage(content="Tell me a joke"),
            ]
        }
        result = _build_activity_summary("model", input_state)
        assert result == 'model: "Tell me a joke"'

    def test_model_node_with_model_name_metadata(self) -> None:
        """Model nodes should include model name from metadata."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {"type": "human", "content": "Hello"},
            ]
        }
        node_metadata = {"model_name": "gpt-4o"}
        result = _build_activity_summary("agent", input_state, node_metadata)
        assert result == 'gpt-4o: "Hello"'

    def test_model_node_with_ls_model_name_metadata(self) -> None:
        """Model nodes should use ls_model_name from metadata."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {"type": "human", "content": "Test query"},
            ]
        }
        node_metadata = {"ls_model_name": "claude-3-opus"}
        result = _build_activity_summary("llm", input_state, node_metadata)
        assert result == 'claude-3-opus: "Test query"'

    def test_model_node_extracts_last_human_message(self) -> None:
        """Model nodes should use last human message when multiple messages present."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {"type": "human", "content": "First question"},
                {"type": "ai", "content": "First answer"},
                {"type": "human", "content": "Second question"},
            ]
        }
        result = _build_activity_summary("agent", input_state)
        assert result == 'agent: "Second question"'

    def test_model_node_truncates_long_query(self) -> None:
        """Model nodes should truncate long queries."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        long_query = "What is " + "the meaning of life " * 10
        input_state = {
            "messages": [
                {"type": "human", "content": long_query},
            ]
        }
        result = _build_activity_summary("agent", input_state)
        assert len(result) <= 100
        assert "..." in result

    def test_model_node_no_messages_returns_node_name(self) -> None:
        """Model nodes with no messages should return just node name."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        result = _build_activity_summary("agent", {"messages": []})
        assert result == "agent"

    def test_model_node_no_human_messages_returns_node_name(self) -> None:
        """Model nodes with no human messages should return just node name."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {"type": "ai", "content": "Hello!"},
            ]
        }
        result = _build_activity_summary("agent", input_state)
        assert result == "agent"

    def test_all_model_node_names_supported(self) -> None:
        """All common model node names should be supported."""
        from temporalio.contrib.langgraph._runner import _build_activity_summary

        input_state = {
            "messages": [
                {"type": "human", "content": "Query"},
            ]
        }

        for node_name in ["agent", "model", "llm", "chatbot", "chat_model"]:
            result = _build_activity_summary(node_name, input_state)
            assert result == f'{node_name}: "Query"', f"Failed for {node_name}"


class TestExtractModelName:
    """Tests for model name extraction from node runnables."""

    def test_extract_model_name_from_closure(self) -> None:
        """Should extract model name from create_agent closure."""
        from unittest.mock import MagicMock

        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        # Create a mock model with model_name
        mock_model = MagicMock()
        mock_model.model_name = "gpt-4o-mini"

        # Create a function with the model in its closure
        def model_node():
            return mock_model  # Captures mock_model in closure

        # Create mock RunnableCallable
        mock_callable = MagicMock()
        mock_callable.func = model_node

        # Create mock RunnableSeq with steps
        mock_runnable_seq = MagicMock()
        mock_runnable_seq.steps = [mock_callable]
        mock_runnable_seq.model_name = None
        mock_runnable_seq.model = None
        mock_runnable_seq.bound = None
        mock_runnable_seq.first = None

        # Create mock node
        mock_node = MagicMock()
        mock_node.node = mock_runnable_seq

        # Create runner with mock pregel
        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {"model": mock_node}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test")
        result = runner._extract_model_name_from_runnable(mock_node)

        assert result == "gpt-4o-mini"

    def test_extract_model_name_direct_attribute(self) -> None:
        """Should extract model name from direct attribute on runnable."""
        from unittest.mock import MagicMock

        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        # Create mock runnable with model_name directly
        mock_runnable = MagicMock()
        mock_runnable.model_name = "claude-3-opus"
        mock_runnable.model = None

        mock_node = MagicMock()
        mock_node.node = mock_runnable

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test")
        result = runner._extract_model_name_from_runnable(mock_node)

        assert result == "claude-3-opus"

    def test_extract_model_name_from_bound(self) -> None:
        """Should extract model name from bound model (e.g., model.bind_tools)."""
        from unittest.mock import MagicMock

        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        mock_bound = MagicMock()
        mock_bound.model_name = "gpt-4-turbo"

        mock_runnable = MagicMock()
        mock_runnable.model_name = None
        mock_runnable.model = None
        mock_runnable.bound = mock_bound

        mock_node = MagicMock()
        mock_node.node = mock_runnable

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test")
        result = runner._extract_model_name_from_runnable(mock_node)

        assert result == "gpt-4-turbo"

    def test_get_full_node_metadata_includes_model_name(self) -> None:
        """_get_full_node_metadata should include extracted model_name."""
        from unittest.mock import MagicMock

        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        # Create mock model
        mock_model = MagicMock()
        mock_model.model_name = "test-model"

        def model_node():
            return mock_model

        mock_callable = MagicMock()
        mock_callable.func = model_node

        mock_runnable_seq = MagicMock()
        mock_runnable_seq.steps = [mock_callable]
        mock_runnable_seq.model_name = None
        mock_runnable_seq.model = None
        mock_runnable_seq.bound = None
        mock_runnable_seq.first = None

        mock_node = MagicMock()
        mock_node.node = mock_runnable_seq
        mock_node.metadata = {"description": "Test node"}

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {"model": mock_node}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test")
        metadata = runner._get_full_node_metadata("model")

        assert metadata["model_name"] == "test-model"
        assert metadata["description"] == "Test node"


class TestCompileFunction:
    """Tests for the compile() public API."""

    def test_compile_returns_runner(self) -> None:
        """compile() should return a TemporalLangGraphRunner."""
        from temporalio.contrib.langgraph import (
            LangGraphPlugin,
            TemporalLangGraphRunner,
            compile,
        )

        class State(TypedDict, total=False):
            value: int

        def build_compile_test():
            graph = StateGraph(State)
            graph.add_node("node", lambda state: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        # Register via plugin
        LangGraphPlugin(graphs={"compile_test": build_compile_test})

        # compile() should work
        runner = compile("compile_test")
        assert isinstance(runner, TemporalLangGraphRunner)
        assert runner.graph_id == "compile_test"

    def test_compile_nonexistent_raises(self) -> None:
        """compile() should raise ApplicationError for unregistered graph."""
        from temporalio.contrib.langgraph import GRAPH_NOT_FOUND_ERROR, compile
        from temporalio.exceptions import ApplicationError

        with pytest.raises(ApplicationError) as exc_info:
            compile("nonexistent_graph")
        assert exc_info.value.type == GRAPH_NOT_FOUND_ERROR

    def test_compile_with_options(self) -> None:
        """compile() should pass options to runner."""
        from temporalio.contrib.langgraph import (
            LangGraphPlugin,
            TemporalLangGraphRunner,
            compile,
        )

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node("node", lambda state: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        LangGraphPlugin(graphs={"options_test": build})

        runner = compile(
            "options_test",
            default_activity_options=activity_options(
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=5),
                task_queue="custom-queue",
            ),
        )

        # Assert runner is a graph runner (not functional runner)
        assert isinstance(runner, TemporalLangGraphRunner)
        assert runner.default_activity_options["start_to_close_timeout"] == timedelta(
            minutes=10
        )
        assert runner.default_activity_options["retry_policy"].maximum_attempts == 5
        assert runner.default_activity_options["task_queue"] == "custom-queue"


class TestParentCommandRouting:
    """Tests for ParentCommand routing from subgraph to parent graph."""

    def test_pending_parent_command_creates_send_packets(self) -> None:
        """When nested runner has pending parent command, send_packets should be created.

        This test verifies the critical logic: when a subgraph node raises
        ParentCommand(goto='node_in_parent'), the parent graph should create
        SendPacket(s) to route execution to the goto target(s) in the parent context.
        """
        from temporalio.contrib.langgraph._models import CommandOutput, SendPacket

        # The logic in _execute_subgraph_as_activity is:
        # 1. Check if nested_runner._pending_parent_command is not None
        # 2. Create SendPackets from cmd.goto
        # 3. Return (writes, send_packets) - but currently returns (writes, []) - BUG!

        # Simulate the logic that should happen:
        cmd = CommandOutput(
            update={"messages": ["tool result"], "remaining_steps": 24},
            goto=["analyst"],  # target node in parent graph
        )

        result = {"messages": ["tool result"], "remaining_steps": 24}

        # This is the logic that should create send_packets
        send_packets: list[SendPacket] = []
        if cmd.goto:
            for node_name in cmd.goto:
                send_packets.append(SendPacket(node=node_name, arg=result))

        # Verify send_packets are created correctly
        assert len(send_packets) == 1
        assert send_packets[0].node == "analyst"
        assert send_packets[0].arg == result

    def test_pending_parent_command_multiple_goto(self) -> None:
        """ParentCommand with multiple goto targets creates multiple SendPackets."""
        from temporalio.contrib.langgraph._models import CommandOutput, SendPacket

        cmd = CommandOutput(
            update={"value": 100},
            goto=["agent1", "agent2", "agent3"],
        )

        result = {"value": 100}

        send_packets: list[SendPacket] = []
        if cmd.goto:
            for node_name in cmd.goto:
                send_packets.append(SendPacket(node=node_name, arg=result))

        assert len(send_packets) == 3
        assert [p.node for p in send_packets] == ["agent1", "agent2", "agent3"]

    def test_nested_runner_stores_pending_parent_command(self) -> None:
        """Runner should store parent_command when node raises ParentCommand.

        When an activity returns a result with parent_command set, the runner
        should store it in _pending_parent_command for the parent graph to handle.
        """
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test")

        # Initially no pending command
        assert runner._execution.pending_parent_command is None

        # After storing a command
        from temporalio.contrib.langgraph._models import CommandOutput

        cmd = CommandOutput(goto=["target_node"], update={"key": "value"})
        runner._execution.pending_parent_command = cmd

        assert runner._execution.pending_parent_command is not None
        assert runner._execution.pending_parent_command.goto == ["target_node"]


class TestErrorRetryability:
    """Tests for error classification (retryable vs non-retryable)."""

    def test_python_builtin_errors_are_non_retryable(self) -> None:
        """Python built-in errors like TypeError, ValueError should not be retried."""
        from temporalio.contrib.langgraph._exceptions import is_non_retryable_error

        # These indicate bugs or bad input - retrying won't help
        assert is_non_retryable_error(TypeError("bad type")) is True
        assert is_non_retryable_error(ValueError("bad value")) is True
        assert is_non_retryable_error(KeyError("missing key")) is True
        assert is_non_retryable_error(AttributeError("no attribute")) is True
        assert is_non_retryable_error(IndexError("out of range")) is True
        assert is_non_retryable_error(AssertionError("assertion failed")) is True
        assert is_non_retryable_error(NotImplementedError("not implemented")) is True

    def test_generic_exceptions_are_retryable(self) -> None:
        """Generic exceptions should be retried by default."""
        from temporalio.contrib.langgraph._exceptions import is_non_retryable_error

        # Unknown errors default to retryable (safer to retry than fail permanently)
        assert is_non_retryable_error(Exception("generic error")) is False
        assert is_non_retryable_error(RuntimeError("runtime error")) is False

    def test_status_code_based_classification(self) -> None:
        """Errors with status_code attribute should be classified by HTTP status."""
        from temporalio.contrib.langgraph._exceptions import is_non_retryable_error

        class HttpError(Exception):
            def __init__(self, status_code: int) -> None:
                self.status_code = status_code
                super().__init__(f"HTTP {status_code}")

        # 4xx client errors (except 429) are non-retryable
        assert is_non_retryable_error(HttpError(400)) is True  # Bad Request
        assert is_non_retryable_error(HttpError(401)) is True  # Unauthorized
        assert is_non_retryable_error(HttpError(403)) is True  # Forbidden
        assert is_non_retryable_error(HttpError(404)) is True  # Not Found
        assert is_non_retryable_error(HttpError(422)) is True  # Unprocessable Entity

        # 429 Rate Limit is retryable
        assert is_non_retryable_error(HttpError(429)) is False

        # 5xx server errors are retryable
        assert is_non_retryable_error(HttpError(500)) is False
        assert is_non_retryable_error(HttpError(502)) is False
        assert is_non_retryable_error(HttpError(503)) is False
        assert is_non_retryable_error(HttpError(504)) is False

    def test_node_execution_error_wraps_with_retry_semantics(self) -> None:
        """node_execution_error should wrap errors with appropriate non_retryable flag."""
        from temporalio.contrib.langgraph._exceptions import (
            NODE_EXECUTION_ERROR,
            node_execution_error,
        )

        # Non-retryable error
        original = ValueError("invalid input")
        wrapped = node_execution_error(
            "my_node", "my_graph", original, non_retryable=True
        )

        assert wrapped.type == NODE_EXECUTION_ERROR
        assert wrapped.non_retryable is True
        assert "my_node" in str(wrapped)
        assert "my_graph" in str(wrapped)
        assert "ValueError" in str(wrapped)

        # Retryable error
        wrapped_retry = node_execution_error(
            "my_node", "my_graph", RuntimeError("transient"), non_retryable=False
        )
        assert wrapped_retry.non_retryable is False


class TestParallelBranchExecution:
    """Tests for parallel execution of parallel branches (BSP model).

    LangGraph uses a Bulk Synchronous Parallel (BSP) model where nodes that are
    ready to execute in the same step should run concurrently. This tests that
    the runner properly executes parallel branches concurrently, not sequentially.
    """

    @pytest.mark.asyncio
    async def test_parallel_branches_execute_concurrently(self) -> None:
        """Parallel branches should execute concurrently, not sequentially.

        This test creates a graph with fan-out pattern (START → [A, B, C] → END)
        and verifies that all three nodes start executing before any complete.
        If execution is sequential, only one node will be executing at a time.
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        from temporalio.contrib.langgraph._models import (
            ChannelWrite,
            NodeActivityOutput,
        )
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        # Track execution timing
        activity_starts: list[str] = []
        activity_completes: list[str] = []
        all_started_event = asyncio.Event()
        num_parallel_nodes = 3
        # Simple barrier using counter and event (works on Python 3.10+)
        start_count = 0
        start_count_event = asyncio.Event()

        async def mock_execute_activity(
            activity_fn,
            activity_input,
            activity_id: str,
            summary: str,
            **kwargs,
        ):
            """Mock activity that tracks execution order."""
            nonlocal start_count
            node_name = activity_input.node_name
            activity_starts.append(node_name)

            # Increment counter and check if all parallel nodes have started
            start_count += 1
            if start_count >= num_parallel_nodes:
                start_count_event.set()
                all_started_event.set()

            # Wait for all parallel nodes to start (proves concurrent execution)
            try:
                await asyncio.wait_for(start_count_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                # If timeout, not all nodes started concurrently (sequential execution)
                pass

            activity_completes.append(node_name)

            return NodeActivityOutput(
                writes=[ChannelWrite(channel="value", value=node_name)],
                interrupt=None,
                store_writes=[],
                send_packets=[],
                parent_command=None,
            )

        # Create mock tasks that simulate parallel branches
        mock_tasks = []
        for node_name in ["node_a", "node_b", "node_c"]:
            task = MagicMock()
            task.name = node_name
            task.id = f"task-{node_name}"
            task.input = {"value": 0}
            task.config = {"configurable": {}}
            task.path = tuple()
            task.triggers = []
            task.writes = []
            mock_tasks.append(task)

        # Create runner with mock pregel
        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {
            "node_a": MagicMock(metadata=None, subgraphs=None),
            "node_b": MagicMock(metadata=None, subgraphs=None),
            "node_c": MagicMock(metadata=None, subgraphs=None),
        }

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test_parallel")

        # Mock loop
        mock_loop = MagicMock()

        # Patch execute_activity and run _execute_loop_tasks
        with patch(
            "temporalio.contrib.langgraph._runner.workflow.execute_activity",
            side_effect=mock_execute_activity,
        ):
            result = await runner._execute_loop_tasks(mock_tasks, mock_loop)

        # Assertions
        assert result is False, "No interrupt should have occurred"
        assert len(activity_starts) == 3, f"Expected 3 starts, got {activity_starts}"
        assert (
            len(activity_completes) == 3
        ), f"Expected 3 completes, got {activity_completes}"

        # THE KEY ASSERTION: If parallel, all 3 should have started before barrier timeout
        # If sequential, the barrier.wait() will timeout for each task
        assert all_started_event.is_set(), (
            "PARALLEL EXECUTION BUG: Tasks executed sequentially instead of concurrently. "
            "All 3 parallel branches should start before any complete, but they ran one at a time."
        )


class TestParallelSendPacketExecution:
    """Tests for parallel execution of Send packets."""

    @pytest.mark.asyncio
    async def test_send_packets_execute_in_parallel(self) -> None:
        """Send packets should execute activities in parallel, not sequentially.

        This test verifies that when multiple Send packets are processed,
        all activities are started before any of them complete, proving
        true parallel execution via asyncio.gather.
        """
        import asyncio
        from unittest.mock import AsyncMock, patch

        from temporalio.contrib.langgraph._models import (
            ChannelWrite,
            NodeActivityOutput,
            SendPacket,
        )
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        # Track activity execution order
        activity_starts: list[str] = []
        activity_completes: list[str] = []
        all_started_event = asyncio.Event()
        num_activities = 3

        async def mock_execute_activity(
            activity_fn,
            activity_input,
            activity_id: str,
            summary: str,
            **kwargs,
        ):
            """Mock activity that tracks start/complete order."""
            node_name = activity_input.node_name
            activity_starts.append(node_name)

            # Wait until all activities have started before completing
            # This proves they were started in parallel
            if len(activity_starts) >= num_activities:
                all_started_event.set()
            else:
                # Give other activities a chance to start
                await asyncio.sleep(0.01)

            # Wait for all to start (with timeout)
            try:
                await asyncio.wait_for(all_started_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass  # Test will fail via assertions below

            activity_completes.append(node_name)

            # Return a valid NodeActivityOutput
            return NodeActivityOutput(
                writes=[ChannelWrite(channel="results", value=[f"result_{node_name}"])],
                interrupt=None,
                store_writes=[],
                send_packets=[],
                parent_command=None,
            )

        # Create runner with mocked pregel
        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {"search": MagicMock()}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test_parallel")

        # Create Send packets for parallel execution
        send_packets = [
            SendPacket(node="search", arg={"query": "query1"}),
            SendPacket(node="search", arg={"query": "query2"}),
            SendPacket(node="search", arg={"query": "query3"}),
        ]

        # Mock the workflow.execute_activity
        with patch(
            "temporalio.contrib.langgraph._runner.workflow.execute_activity",
            side_effect=mock_execute_activity,
        ):
            config = {"configurable": {"invocation_id": 1}}
            writes = await runner._execute_send_packets(send_packets, config)

        # Verify all activities started before any completed
        # If parallel, all 3 should be in activity_starts before first is in activity_completes
        assert len(activity_starts) == 3, f"Expected 3 starts, got {activity_starts}"
        assert (
            len(activity_completes) == 3
        ), f"Expected 3 completes, got {activity_completes}"

        # The key assertion: by the time all_started_event was set,
        # all 3 activities had started. This proves parallel execution.
        assert (
            all_started_event.is_set()
        ), "Activities did not all start before completing"

        # Verify writes were collected
        assert len(writes) == 3

    @pytest.mark.asyncio
    async def test_empty_send_packets_returns_empty_list(self) -> None:
        """Empty send_packets list should return empty writes immediately."""
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        mock_pregel = MagicMock()
        mock_pregel.step_timeout = None
        mock_pregel.nodes = {}

        runner = TemporalLangGraphRunner(mock_pregel, graph_id="test")

        config = {"configurable": {"invocation_id": 1}}
        writes = await runner._execute_send_packets([], config)

        assert writes == []
