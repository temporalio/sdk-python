"""Tests for LangGraph-Temporal integration (Phase 2).

These tests validate the production implementation:
- Models (ChannelWrite, NodeActivityInput, NodeActivityOutput)
- Graph registry
- Plugin
- Runner
- End-to-end workflow tests with real Temporal worker
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph
from temporalio.client import Client


class TestModels:
    """Tests for Pydantic models."""

    def test_channel_write_basic(self) -> None:
        """ChannelWrite should store channel and value."""
        from temporalio.contrib.langgraph._models import ChannelWrite

        write = ChannelWrite(channel="output", value=42)
        assert write.channel == "output"
        assert write.value == 42
        assert write.value_type is None

    def test_channel_write_create_detects_message(self) -> None:
        """ChannelWrite.create should detect LangChain messages."""
        from langchain_core.messages import HumanMessage

        from temporalio.contrib.langgraph._models import ChannelWrite

        msg = HumanMessage(content="Hello")
        write = ChannelWrite.create("messages", msg)

        assert write.channel == "messages"
        assert write.value_type == "message"

    def test_channel_write_create_detects_message_list(self) -> None:
        """ChannelWrite.create should detect list of messages."""
        from langchain_core.messages import AIMessage, HumanMessage

        from temporalio.contrib.langgraph._models import ChannelWrite

        messages = [HumanMessage(content="Hi"), AIMessage(content="Hello")]
        write = ChannelWrite.create("messages", messages)

        assert write.value_type == "message_list"

    def test_channel_write_create_regular_value(self) -> None:
        """ChannelWrite.create should handle regular values."""
        from temporalio.contrib.langgraph._models import ChannelWrite

        write = ChannelWrite.create("count", 10)

        assert write.channel == "count"
        assert write.value == 10
        assert write.value_type is None

    def test_channel_write_reconstruct_message(self) -> None:
        """ChannelWrite should reconstruct messages from dicts."""
        from temporalio.contrib.langgraph._models import ChannelWrite

        # Simulate serialized message (as dict)
        serialized = {"content": "Hello", "type": "human"}
        write = ChannelWrite(channel="messages", value=serialized, value_type="message")

        reconstructed = write.reconstruct_value()
        assert reconstructed.content == "Hello"
        assert type(reconstructed).__name__ == "HumanMessage"

    def test_channel_write_to_tuple(self) -> None:
        """ChannelWrite.to_tuple should return (channel, value)."""
        from temporalio.contrib.langgraph._models import ChannelWrite

        write = ChannelWrite(channel="output", value="result")
        assert write.to_tuple() == ("output", "result")

    def test_node_activity_input(self) -> None:
        """NodeActivityInput should store all required fields."""
        from temporalio.contrib.langgraph._models import NodeActivityInput

        input_data = NodeActivityInput(
            node_name="my_node",
            task_id="task_123",
            graph_id="my_graph",
            input_state={"value": 1},
            config={"key": "value"},
            path=("graph", "subgraph"),
            triggers=["input"],
        )

        assert input_data.node_name == "my_node"
        assert input_data.task_id == "task_123"
        assert input_data.graph_id == "my_graph"
        assert input_data.input_state == {"value": 1}

    def test_node_activity_output(self) -> None:
        """NodeActivityOutput should store writes."""
        from temporalio.contrib.langgraph._models import (
            ChannelWrite,
            NodeActivityOutput,
        )

        output = NodeActivityOutput(
            writes=[
                ChannelWrite(channel="a", value=1),
                ChannelWrite(channel="b", value=2),
            ]
        )

        assert len(output.writes) == 2
        tuples = output.to_write_tuples()
        assert tuples == [("a", 1), ("b", 2)]


class TestGraphRegistry:
    """Tests for the graph registry."""

    def test_register_and_get(self) -> None:
        """Registry should cache graph after first access."""
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        class State(TypedDict, total=False):
            value: int

        def build_graph():
            graph = StateGraph(State)
            graph.add_node("node", lambda s: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        registry = GraphRegistry()
        registry.register("test_graph", build_graph)

        # First access builds
        graph1 = registry.get_graph("test_graph")
        assert graph1 is not None

        # Second access returns cached
        graph2 = registry.get_graph("test_graph")
        assert graph1 is graph2

    def test_get_nonexistent_raises(self) -> None:
        """Getting nonexistent graph should raise KeyError."""
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        registry = GraphRegistry()

        with pytest.raises(KeyError, match="not found"):
            registry.get_graph("nonexistent")

    def test_register_duplicate_raises(self) -> None:
        """Registering duplicate graph ID should raise ValueError."""
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        registry = GraphRegistry()
        registry.register("dup", lambda: MagicMock())

        with pytest.raises(ValueError, match="already registered"):
            registry.register("dup", lambda: MagicMock())

    def test_get_node(self) -> None:
        """Registry should allow getting specific nodes."""
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        class State(TypedDict, total=False):
            value: int

        def my_node(s: State) -> State:
            return {"value": s.get("value", 0) + 1}

        def build_graph():
            graph = StateGraph(State)
            graph.add_node("my_node", my_node)
            graph.add_edge(START, "my_node")
            graph.add_edge("my_node", END)
            return graph.compile()

        registry = GraphRegistry()
        registry.register("test_graph", build_graph)

        node = registry.get_node("test_graph", "my_node")
        assert node is not None

    def test_list_graphs(self) -> None:
        """Registry should list registered graph IDs."""
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        registry = GraphRegistry()
        registry.register("graph_a", lambda: MagicMock())
        registry.register("graph_b", lambda: MagicMock())

        graphs = registry.list_graphs()
        assert "graph_a" in graphs
        assert "graph_b" in graphs

    def test_clear(self) -> None:
        """Registry clear should remove all entries."""
        from temporalio.contrib.langgraph._graph_registry import GraphRegistry

        registry = GraphRegistry()
        registry.register("graph", lambda: MagicMock())
        registry.clear()

        assert not registry.is_registered("graph")


class TestLangGraphPlugin:
    """Tests for the LangGraph plugin."""

    def test_plugin_registers_graphs(self) -> None:
        """Plugin should register graphs in global registry."""
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._plugin import LangGraphPlugin

        # Clear global registry first
        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def build_test_graph():
            graph = StateGraph(State)
            graph.add_node("node", lambda s: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        plugin = LangGraphPlugin(
            graphs={"plugin_test_graph": build_test_graph},
        )

        assert plugin.is_graph_registered("plugin_test_graph")
        assert "plugin_test_graph" in plugin.get_graph_ids()

    def test_plugin_default_timeout(self) -> None:
        """Plugin should have default timeout."""
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._plugin import LangGraphPlugin

        get_global_registry().clear()

        plugin = LangGraphPlugin(
            graphs={},
            default_activity_timeout=timedelta(minutes=10),
        )

        assert plugin.default_activity_timeout == timedelta(minutes=10)


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
        assert runner.default_activity_timeout == timedelta(minutes=5)

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


class TestCompileFunction:
    """Tests for the compile() public API."""

    def test_compile_returns_runner(self) -> None:
        """compile() should return a TemporalLangGraphRunner."""
        from temporalio.contrib.langgraph import (
            LangGraphPlugin,
            TemporalLangGraphRunner,
            compile,
        )
        from temporalio.contrib.langgraph._graph_registry import get_global_registry

        # Clear and setup
        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def build_compile_test():
            graph = StateGraph(State)
            graph.add_node("node", lambda s: {"value": 1})
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
        """compile() should raise KeyError for unregistered graph."""
        from temporalio.contrib.langgraph import compile
        from temporalio.contrib.langgraph._graph_registry import get_global_registry

        get_global_registry().clear()

        with pytest.raises(KeyError, match="not found"):
            compile("nonexistent_graph")

    def test_compile_with_options(self) -> None:
        """compile() should pass options to runner."""
        from temporalio.contrib.langgraph import LangGraphPlugin, compile
        from temporalio.contrib.langgraph._graph_registry import get_global_registry

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node("node", lambda s: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        LangGraphPlugin(graphs={"options_test": build})

        runner = compile(
            "options_test",
            default_activity_timeout=timedelta(minutes=10),
            default_max_retries=5,
            default_task_queue="custom-queue",
            enable_workflow_execution=True,
        )

        assert runner.default_activity_timeout == timedelta(minutes=10)
        assert runner.default_max_retries == 5
        assert runner.default_task_queue == "custom-queue"
        assert runner.enable_workflow_execution is True


class TestNodeExecutionActivity:
    """Tests for the node execution activity."""

    def test_activity_captures_writes_via_config_key_send(self) -> None:
        """Activity should capture writes via CONFIG_KEY_SEND callback."""
        import asyncio

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._activities import execute_node
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._models import NodeActivityInput

        get_global_registry().clear()

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
        import asyncio

        from langchain_core.messages import AIMessage, HumanMessage

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._activities import execute_node
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._models import NodeActivityInput

        get_global_registry().clear()

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
        import asyncio

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._activities import execute_node
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._models import NodeActivityInput

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node("real_node", lambda s: {"value": 1})
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
                asyncio.get_event_loop().run_until_complete(
                    execute_node(input_data)
                )


class TestPerNodeConfiguration:
    """Tests for per-node configuration (Phase 4)."""

    def test_node_timeout_from_metadata(self) -> None:
        """Runner should read activity_timeout from node metadata."""
        from unittest.mock import MagicMock

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node(
                "slow_node",
                lambda s: {"value": 1},
                metadata={"temporal": {"activity_timeout": timedelta(hours=2)}},
            )
            graph.add_node(
                "fast_node",
                lambda s: {"value": 2},
                # No metadata - should use default
            )
            graph.add_edge(START, "slow_node")
            graph.add_edge("slow_node", "fast_node")
            graph.add_edge("fast_node", END)
            return graph.compile()

        LangGraphPlugin(graphs={"timeout_test": build})
        pregel = get_global_registry().get_graph("timeout_test")

        runner = TemporalLangGraphRunner(
            pregel,
            graph_id="timeout_test",
            default_activity_timeout=timedelta(minutes=5),
        )

        # Check timeouts
        assert runner._get_node_timeout("slow_node") == timedelta(hours=2)
        assert runner._get_node_timeout("fast_node") == timedelta(minutes=5)

    def test_node_task_queue_from_metadata(self) -> None:
        """Runner should read task_queue from node metadata."""
        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node(
                "gpu_node",
                lambda s: {"value": 1},
                metadata={"temporal": {"task_queue": "gpu-workers"}},
            )
            graph.add_node(
                "cpu_node",
                lambda s: {"value": 2},
            )
            graph.add_edge(START, "gpu_node")
            graph.add_edge("gpu_node", "cpu_node")
            graph.add_edge("cpu_node", END)
            return graph.compile()

        LangGraphPlugin(graphs={"queue_test": build})
        pregel = get_global_registry().get_graph("queue_test")

        runner = TemporalLangGraphRunner(
            pregel,
            graph_id="queue_test",
            default_task_queue="standard-workers",
        )

        assert runner._get_node_task_queue("gpu_node") == "gpu-workers"
        assert runner._get_node_task_queue("cpu_node") == "standard-workers"

    def test_node_retry_policy_mapping(self) -> None:
        """Runner should map LangGraph RetryPolicy to Temporal RetryPolicy."""
        from langgraph.types import RetryPolicy as LGRetryPolicy

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node(
                "flaky_node",
                lambda s: {"value": 1},
                retry_policy=LGRetryPolicy(
                    max_attempts=5,
                    initial_interval=2.0,
                    backoff_factor=3.0,
                    max_interval=120.0,
                ),
            )
            graph.add_node(
                "reliable_node",
                lambda s: {"value": 2},
            )
            graph.add_edge(START, "flaky_node")
            graph.add_edge("flaky_node", "reliable_node")
            graph.add_edge("reliable_node", END)
            return graph.compile()

        LangGraphPlugin(graphs={"retry_test": build})
        pregel = get_global_registry().get_graph("retry_test")

        runner = TemporalLangGraphRunner(
            pregel,
            graph_id="retry_test",
            default_max_retries=3,
        )

        # Check flaky node has custom retry policy
        flaky_policy = runner._get_node_retry_policy("flaky_node")
        assert flaky_policy.maximum_attempts == 5
        assert flaky_policy.initial_interval == timedelta(seconds=2)
        assert flaky_policy.backoff_coefficient == 3.0
        assert flaky_policy.maximum_interval == timedelta(seconds=120)

        # Check reliable node uses default
        reliable_policy = runner._get_node_retry_policy("reliable_node")
        assert reliable_policy.maximum_attempts == 3

    def test_node_heartbeat_timeout_from_metadata(self) -> None:
        """Runner should read heartbeat_timeout from node metadata."""
        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node(
                "long_running",
                lambda s: {"value": 1},
                metadata={
                    "temporal": {
                        "activity_timeout": timedelta(hours=1),
                        "heartbeat_timeout": timedelta(minutes=5),
                    }
                },
            )
            graph.add_node(
                "short_running",
                lambda s: {"value": 2},
            )
            graph.add_edge(START, "long_running")
            graph.add_edge("long_running", "short_running")
            graph.add_edge("short_running", END)
            return graph.compile()

        LangGraphPlugin(graphs={"heartbeat_test": build})
        pregel = get_global_registry().get_graph("heartbeat_test")

        runner = TemporalLangGraphRunner(
            pregel,
            graph_id="heartbeat_test",
        )

        assert runner._get_node_heartbeat_timeout("long_running") == timedelta(minutes=5)
        assert runner._get_node_heartbeat_timeout("short_running") is None


class TestInterruptHandling:
    """Tests for human-in-the-loop interrupt functionality."""

    def test_interrupt_value_model(self) -> None:
        """InterruptValue should store interrupt data."""
        from temporalio.contrib.langgraph._models import InterruptValue

        interrupt = InterruptValue(
            value="Please confirm",
            node_name="confirm_node",
            task_id="task_456",
        )

        assert interrupt.value == "Please confirm"
        assert interrupt.node_name == "confirm_node"
        assert interrupt.task_id == "task_456"

    def test_node_activity_output_with_interrupt(self) -> None:
        """NodeActivityOutput should support interrupt field."""
        from temporalio.contrib.langgraph._models import (
            InterruptValue,
            NodeActivityOutput,
        )

        output = NodeActivityOutput(
            writes=[],
            interrupt=InterruptValue(
                value="waiting",
                node_name="wait_node",
                task_id="task_789",
            ),
        )

        assert output.interrupt is not None
        assert output.interrupt.value == "waiting"
        assert len(output.writes) == 0

    def test_node_activity_input_with_resume(self) -> None:
        """NodeActivityInput should support resume_value field."""
        from temporalio.contrib.langgraph._models import NodeActivityInput

        input_data = NodeActivityInput(
            node_name="my_node",
            task_id="task_123",
            graph_id="my_graph",
            input_state={"value": 1},
            config={},
            path=(),
            triggers=[],
            resume_value="user_response",
        )

        assert input_data.resume_value == "user_response"

    def test_activity_catches_langgraph_interrupt(self) -> None:
        """Activity should catch LangGraph interrupt and return InterruptValue."""
        import asyncio

        from langgraph.types import interrupt

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._activities import execute_node
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._models import NodeActivityInput

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int
            approved: bool

        def approval_node(state: State) -> State:
            # This will raise GraphInterrupt
            approved = interrupt({"question": "Do you approve?", "value": state.get("value")})
            return {"approved": approved}

        def build():
            graph = StateGraph(State)
            graph.add_node("approval", approval_node)
            graph.add_edge(START, "approval")
            graph.add_edge("approval", END)
            return graph.compile()

        LangGraphPlugin(graphs={"interrupt_test": build})

        input_data = NodeActivityInput(
            node_name="approval",
            task_id="test_task_interrupt",
            graph_id="interrupt_test",
            input_state={"value": 42},
            config={},
            path=(),
            triggers=[],
        )

        with patch("temporalio.activity.heartbeat"):
            result = asyncio.get_event_loop().run_until_complete(
                execute_node(input_data)
            )

        # Should return interrupt, not writes
        assert result.interrupt is not None
        assert result.interrupt.node_name == "approval"
        assert result.interrupt.value == {"question": "Do you approve?", "value": 42}
        assert len(result.writes) == 0

    def test_activity_resumes_with_value(self) -> None:
        """Activity should pass resume value to interrupt()."""
        import asyncio

        from langgraph.types import interrupt

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._activities import execute_node
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._models import NodeActivityInput

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int
            approved: bool

        def approval_node(state: State) -> State:
            # When resume_value is provided, interrupt() returns it
            approved = interrupt("Approve?")
            return {"approved": approved}

        def build():
            graph = StateGraph(State)
            graph.add_node("approval", approval_node)
            graph.add_edge(START, "approval")
            graph.add_edge("approval", END)
            return graph.compile()

        LangGraphPlugin(graphs={"resume_test": build})

        # Execute with resume_value - should NOT raise interrupt
        input_data = NodeActivityInput(
            node_name="approval",
            task_id="test_task_resume",
            graph_id="resume_test",
            input_state={"value": 42},
            config={},
            path=(),
            triggers=[],
            resume_value=True,  # Resume with approval
        )

        with patch("temporalio.activity.heartbeat"):
            result = asyncio.get_event_loop().run_until_complete(
                execute_node(input_data)
            )

        # Should return writes, not interrupt
        assert result.interrupt is None
        # Filter out internal LangGraph channels (like __resume__)
        user_writes = [w for w in result.writes if not w.channel.startswith("__")]
        assert len(user_writes) == 1
        assert user_writes[0].channel == "approved"
        assert user_writes[0].value is True

    def test_runner_stores_interrupted_state(self) -> None:
        """Runner should initialize interrupt state tracking."""
        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node("node", lambda s: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        LangGraphPlugin(graphs={"state_test": build})
        pregel = get_global_registry().get_graph("state_test")

        runner = TemporalLangGraphRunner(pregel, graph_id="state_test")

        # Should have interrupt state attributes
        assert runner._interrupted_state is None
        assert runner._resume_value is None
        assert runner._resume_used is False

    def test_runner_has_pending_interrupt_attribute(self) -> None:
        """Runner should have _pending_interrupt attribute for native API."""
        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def build():
            graph = StateGraph(State)
            graph.add_node("node", lambda s: {"value": 1})
            graph.add_edge(START, "node")
            graph.add_edge("node", END)
            return graph.compile()

        LangGraphPlugin(graphs={"pending_test": build})
        pregel = get_global_registry().get_graph("pending_test")

        runner = TemporalLangGraphRunner(pregel, graph_id="pending_test")

        # Should have _pending_interrupt attribute for native API
        assert runner._pending_interrupt is None


class TestInterruptIntegration:
    """Integration tests for interrupt functionality."""

    def test_ainvoke_returns_interrupt_in_result(self) -> None:
        """ainvoke should return __interrupt__ in result when node calls interrupt()."""
        import asyncio
        from unittest.mock import AsyncMock

        from langgraph.types import interrupt

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._models import (
            InterruptValue,
            NodeActivityOutput,
        )
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int
            approved: bool

        def approval_node(state: State) -> State:
            approved = interrupt({"question": "Do you approve?", "value": state.get("value")})
            return {"approved": approved}

        def build():
            graph = StateGraph(State)
            graph.add_node("approval", approval_node)
            graph.add_edge(START, "approval")
            graph.add_edge("approval", END)
            return graph.compile()

        LangGraphPlugin(graphs={"int_test_1": build})
        pregel = get_global_registry().get_graph("int_test_1")
        runner = TemporalLangGraphRunner(pregel, graph_id="int_test_1")

        # Mock workflow.execute_activity to return an interrupt
        mock_result = NodeActivityOutput(
            writes=[],
            interrupt=InterruptValue(
                value={"question": "Do you approve?", "value": 42},
                node_name="approval",
                task_id="task_123",
            ),
        )

        async def run_test():
            with patch("temporalio.contrib.langgraph._runner.workflow") as mock_workflow:
                mock_workflow.execute_activity = AsyncMock(return_value=mock_result)
                mock_workflow.unsafe = MagicMock()
                mock_workflow.unsafe.imports_passed_through = MagicMock(
                    return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock())
                )

                result = await runner.ainvoke({"value": 42})

                # Result should contain __interrupt__ key
                assert "__interrupt__" in result
                assert len(result["__interrupt__"]) == 1

                interrupt_obj = result["__interrupt__"][0]
                assert interrupt_obj.value == {"question": "Do you approve?", "value": 42}

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_ainvoke_resumes_with_command(self) -> None:
        """ainvoke should resume execution when called with Command(resume=value)."""
        import asyncio
        from unittest.mock import AsyncMock

        from langgraph.types import Command, interrupt

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._models import (
            ChannelWrite,
            InterruptValue,
            NodeActivityOutput,
        )
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int
            approved: bool

        def approval_node(state: State) -> State:
            approved = interrupt("Approve?")
            return {"approved": approved}

        def build():
            graph = StateGraph(State)
            graph.add_node("approval", approval_node)
            graph.add_edge(START, "approval")
            graph.add_edge("approval", END)
            return graph.compile()

        LangGraphPlugin(graphs={"int_test_2": build})
        pregel = get_global_registry().get_graph("int_test_2")
        runner = TemporalLangGraphRunner(pregel, graph_id="int_test_2")

        call_count = 0

        async def mock_execute_activity(func, input_data, **kwargs):
            nonlocal call_count
            call_count += 1

            if call_count == 1:
                # First call: return interrupt
                return NodeActivityOutput(
                    writes=[],
                    interrupt=InterruptValue(
                        value="Approve?",
                        node_name="approval",
                        task_id="task_456",
                    ),
                )
            else:
                # Second call (resume): verify resume_value is passed
                assert input_data.resume_value is True, f"Expected resume_value=True, got {input_data.resume_value}"
                return NodeActivityOutput(
                    writes=[ChannelWrite(channel="approved", value=True)],
                    interrupt=None,
                )

        async def run_test():
            with patch("temporalio.contrib.langgraph._runner.workflow") as mock_workflow:
                mock_workflow.execute_activity = mock_execute_activity
                mock_workflow.unsafe = MagicMock()
                mock_workflow.unsafe.imports_passed_through = MagicMock(
                    return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock())
                )

                # First call - should return interrupt
                result1 = await runner.ainvoke({"value": 42})
                assert "__interrupt__" in result1
                assert result1["__interrupt__"][0].value == "Approve?"

                # Verify state was saved
                assert runner._interrupted_state is not None
                assert runner._pending_interrupt is not None

                # Second call with Command(resume=True) - should resume
                result2 = await runner.ainvoke(Command(resume=True))

                # Should complete without interrupt
                assert "__interrupt__" not in result2
                assert call_count == 2

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_interrupt_state_reset_on_resume(self) -> None:
        """Interrupt state should be reset after successful resume."""
        import asyncio
        from unittest.mock import AsyncMock

        from langgraph.types import Command

        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from temporalio.contrib.langgraph._models import (
            ChannelWrite,
            InterruptValue,
            NodeActivityOutput,
        )
        from temporalio.contrib.langgraph._runner import TemporalLangGraphRunner

        get_global_registry().clear()

        class State(TypedDict, total=False):
            value: int

        def simple_node(state: State) -> State:
            return {"value": state.get("value", 0) + 1}

        def build():
            graph = StateGraph(State)
            graph.add_node("simple", simple_node)
            graph.add_edge(START, "simple")
            graph.add_edge("simple", END)
            return graph.compile()

        LangGraphPlugin(graphs={"int_test_3": build})
        pregel = get_global_registry().get_graph("int_test_3")
        runner = TemporalLangGraphRunner(pregel, graph_id="int_test_3")

        # Manually set interrupt state to simulate previous interrupt
        runner._interrupted_state = {"value": 42}
        runner._pending_interrupt = InterruptValue(
            value="test",
            node_name="test_node",
            task_id="task_789",
        )

        async def mock_execute_activity(func, input_data, **kwargs):
            return NodeActivityOutput(
                writes=[ChannelWrite(channel="value", value=43)],
                interrupt=None,
            )

        async def run_test():
            with patch("temporalio.contrib.langgraph._runner.workflow") as mock_workflow:
                mock_workflow.execute_activity = mock_execute_activity
                mock_workflow.unsafe = MagicMock()
                mock_workflow.unsafe.imports_passed_through = MagicMock(
                    return_value=MagicMock(__enter__=MagicMock(), __exit__=MagicMock())
                )

                # Resume execution
                result = await runner.ainvoke(Command(resume="user_input"))

                # Interrupt state should be cleared after successful execution
                assert "__interrupt__" not in result
                # _pending_interrupt is reset at start of ainvoke when Command is passed
                assert runner._pending_interrupt is None

        asyncio.get_event_loop().run_until_complete(run_test())


# ==============================================================================
# End-to-End Tests with Real Temporal Worker
# ==============================================================================

# Graph builders and workflows must be defined at module level for Temporal

from temporalio import workflow
from temporalio.contrib.langgraph import LangGraphPlugin, compile as lg_compile
from langgraph.types import Command


class E2EApprovalState(TypedDict, total=False):
    """State for approval workflow."""

    value: int
    approved: bool
    approval_reason: str


def _e2e_approval_node(state: E2EApprovalState) -> E2EApprovalState:
    """Node that requests approval via interrupt."""
    from langgraph.types import interrupt

    # Request approval - this will pause execution
    approval_response = interrupt({
        "question": "Do you approve this value?",
        "current_value": state.get("value", 0),
    })

    # When resumed, approval_response will be the value passed to Command(resume=...)
    return {
        "approved": approval_response.get("approved", False),
        "approval_reason": approval_response.get("reason", ""),
    }


def _e2e_process_node(state: E2EApprovalState) -> E2EApprovalState:
    """Node that processes the approved value."""
    if state.get("approved"):
        return {"value": state.get("value", 0) * 2}
    return {"value": 0}


def build_e2e_approval_graph():
    """Build the approval graph for e2e tests."""
    graph = StateGraph(E2EApprovalState)
    graph.add_node("request_approval", _e2e_approval_node)
    graph.add_node("process", _e2e_process_node)
    graph.add_edge(START, "request_approval")
    graph.add_edge("request_approval", "process")
    graph.add_edge("process", END)
    return graph.compile()


class E2ESimpleState(TypedDict, total=False):
    """State for simple workflow without interrupts."""

    value: int
    result: int


def _e2e_double_node(state: E2ESimpleState) -> E2ESimpleState:
    """Simple node that doubles the value."""
    return {"result": state.get("value", 0) * 2}


def build_e2e_simple_graph():
    """Build a simple graph without interrupts for e2e tests."""
    graph = StateGraph(E2ESimpleState)
    graph.add_node("double", _e2e_double_node)
    graph.add_edge(START, "double")
    graph.add_edge("double", END)
    return graph.compile()


# Module-level workflow definitions for e2e tests
# Using sandboxed=False because langgraph imports aren't sandbox-compatible
@workflow.defn(sandboxed=False)
class E2ESimpleGraphWorkflow:
    """Simple workflow for e2e testing."""

    @workflow.run
    async def run(self, input_value: int) -> dict:
        app = lg_compile("e2e_simple")
        return await app.ainvoke({"value": input_value})


@workflow.defn(sandboxed=False)
class E2EApprovalWorkflow:
    """Workflow with interrupt for e2e testing."""

    def __init__(self):
        self._approval_response: dict | None = None
        self._interrupt_value: Any = None

    @workflow.signal
    def provide_approval(self, response: dict) -> None:
        self._approval_response = response

    @workflow.query
    def get_interrupt_value(self) -> Any:
        return self._interrupt_value

    @workflow.run
    async def run(self, input_value: int) -> dict:
        app = lg_compile("e2e_approval")

        # First invocation - should hit interrupt
        result = await app.ainvoke({"value": input_value})

        # Check for interrupt
        if "__interrupt__" in result:
            self._interrupt_value = result["__interrupt__"][0].value

            # Wait for signal with approval
            await workflow.wait_condition(
                lambda: self._approval_response is not None
            )

            # Resume with the approval response
            result = await app.ainvoke(Command(resume=self._approval_response))

        return result


@workflow.defn(sandboxed=False)
class E2ERejectionWorkflow:
    """Workflow for testing interrupt rejection."""

    def __init__(self):
        self._approval_response: dict | None = None

    @workflow.signal
    def provide_approval(self, response: dict) -> None:
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


class TestE2EWorkflows:
    """End-to-end tests with real Temporal worker."""

    @pytest.mark.asyncio
    async def test_simple_graph_execution(self, client: Client) -> None:
        """Test basic graph execution without interrupts."""
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from tests.helpers import new_worker

        # Clear registry to avoid conflicts
        get_global_registry().clear()

        # Create plugin with the graph
        plugin = LangGraphPlugin(
            graphs={"e2e_simple": build_e2e_simple_graph},
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
            E2ESimpleGraphWorkflow,
        ) as worker:
            result = await plugin_client.execute_workflow(
                E2ESimpleGraphWorkflow.run,
                21,
                id=f"e2e-simple-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            assert result["result"] == 42

    @pytest.mark.asyncio
    async def test_interrupt_and_resume_with_signal(self, client: Client) -> None:
        """Test interrupt flow with signal-based resume."""
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from tests.helpers import new_worker
        import asyncio

        # Clear registry to avoid conflicts
        get_global_registry().clear()

        # Create plugin with the approval graph
        plugin = LangGraphPlugin(
            graphs={"e2e_approval": build_e2e_approval_graph},
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
            E2EApprovalWorkflow,
        ) as worker:
            # Start workflow
            handle = await plugin_client.start_workflow(
                E2EApprovalWorkflow.run,
                42,
                id=f"e2e-approval-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )

            # Wait a bit for the workflow to reach the interrupt
            await asyncio.sleep(1)

            # Query the interrupt value
            interrupt_value = await handle.query(E2EApprovalWorkflow.get_interrupt_value)
            assert interrupt_value is not None
            assert interrupt_value["question"] == "Do you approve this value?"
            assert interrupt_value["current_value"] == 42

            # Send approval signal
            await handle.signal(
                E2EApprovalWorkflow.provide_approval,
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
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from tests.helpers import new_worker
        import asyncio

        # Clear registry to avoid conflicts
        get_global_registry().clear()

        # Create plugin with the approval graph
        plugin = LangGraphPlugin(
            graphs={"e2e_approval_reject": build_e2e_approval_graph},
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
            E2ERejectionWorkflow,
        ) as worker:
            handle = await plugin_client.start_workflow(
                E2ERejectionWorkflow.run,
                100,
                id=f"e2e-reject-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )

            await asyncio.sleep(1)

            # Reject the approval
            await handle.signal(
                E2ERejectionWorkflow.provide_approval,
                {"approved": False, "reason": "Not approved"},
            )

            result = await handle.result()

            # Value should be 0 (rejected)
            assert result["value"] == 0
            assert result["approved"] is False
