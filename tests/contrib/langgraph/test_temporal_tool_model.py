"""Tests for temporal_tool and temporal_model functionality.

These tests validate:
- Tool wrapping with temporal_tool()
- Model wrapping with temporal_model()
- Tool and model registries
- Activity execution for tools and models
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typing_extensions import TypedDict

from temporalio.common import RetryPolicy


# ==============================================================================
# Tool Registry Tests
# ==============================================================================


class TestToolRegistry:
    """Tests for the tool registry."""

    def test_register_and_get_tool(self) -> None:
        """Should register and retrieve tools by name."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph._tool_registry import (
            clear_registry,
            get_tool,
            register_tool,
        )

        clear_registry()

        @tool
        def my_tool(query: str) -> str:
            """A test tool."""
            return f"Result: {query}"

        register_tool(my_tool)

        retrieved = get_tool("my_tool")
        assert retrieved is my_tool

    def test_get_nonexistent_tool_raises(self) -> None:
        """Should raise KeyError for unregistered tools."""
        from temporalio.contrib.langgraph._tool_registry import (
            clear_registry,
            get_tool,
        )

        clear_registry()

        with pytest.raises(KeyError, match="not found"):
            get_tool("nonexistent_tool")

    def test_register_duplicate_tool_same_instance(self) -> None:
        """Should allow re-registering the same tool instance."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph._tool_registry import (
            clear_registry,
            get_tool,
            register_tool,
        )

        clear_registry()

        @tool
        def my_tool(query: str) -> str:
            """A test tool."""
            return query

        register_tool(my_tool)
        register_tool(my_tool)  # Same instance, should not raise

        assert get_tool("my_tool") is my_tool

    def test_get_all_tools(self) -> None:
        """Should return all registered tools."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph._tool_registry import (
            clear_registry,
            get_all_tools,
            register_tool,
        )

        clear_registry()

        @tool
        def tool_a(x: str) -> str:
            """Tool A."""
            return x

        @tool
        def tool_b(x: str) -> str:
            """Tool B."""
            return x

        register_tool(tool_a)
        register_tool(tool_b)

        all_tools = get_all_tools()
        assert "tool_a" in all_tools
        assert "tool_b" in all_tools


# ==============================================================================
# Model Registry Tests
# ==============================================================================


class TestModelRegistry:
    """Tests for the model registry."""

    def test_register_and_get_model(self) -> None:
        """Should register and retrieve models by name."""
        from temporalio.contrib.langgraph._model_registry import (
            clear_registry,
            get_model,
            register_model,
        )

        clear_registry()

        # Create a mock model
        mock_model = MagicMock()
        mock_model.model_name = "test-model"

        register_model(mock_model)

        retrieved = get_model("test-model")
        assert retrieved is mock_model

    def test_register_model_with_explicit_name(self) -> None:
        """Should register model with explicit name."""
        from temporalio.contrib.langgraph._model_registry import (
            clear_registry,
            get_model,
            register_model,
        )

        clear_registry()

        mock_model = MagicMock()
        register_model(mock_model, name="custom-name")

        retrieved = get_model("custom-name")
        assert retrieved is mock_model

    def test_get_nonexistent_model_raises(self) -> None:
        """Should raise KeyError for unregistered models."""
        from temporalio.contrib.langgraph._model_registry import (
            clear_registry,
            get_model,
        )

        clear_registry()

        with pytest.raises(KeyError, match="not found"):
            get_model("nonexistent-model")

    def test_register_model_factory(self) -> None:
        """Should support lazy model instantiation via factory."""
        from temporalio.contrib.langgraph._model_registry import (
            clear_registry,
            get_model,
            register_model_factory,
        )

        clear_registry()

        mock_model = MagicMock()
        factory_called = False

        def model_factory():
            nonlocal factory_called
            factory_called = True
            return mock_model

        register_model_factory("lazy-model", model_factory)

        # Factory not called yet
        assert factory_called is False

        # Get model - factory should be called
        retrieved = get_model("lazy-model")
        assert factory_called is True
        assert retrieved is mock_model

        # Second get should use cached instance
        factory_called = False
        retrieved2 = get_model("lazy-model")
        assert factory_called is False
        assert retrieved2 is mock_model


# ==============================================================================
# temporal_tool() Tests
# ==============================================================================


class TestTemporalTool:
    """Tests for the temporal_tool() wrapper."""

    def test_wrap_tool_preserves_metadata(self) -> None:
        """Wrapped tool should preserve name, description, args_schema."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_tool
        from temporalio.contrib.langgraph._tool_registry import clear_registry

        clear_registry()

        @tool
        def search_web(query: str) -> str:
            """Search the web for information."""
            return f"Results for: {query}"

        wrapped = temporal_tool(
            search_web,
            start_to_close_timeout=timedelta(minutes=2),
        )

        assert wrapped.name == "search_web"
        assert wrapped.description == "Search the web for information."

    def test_wrap_tool_with_all_options(self) -> None:
        """Should accept all activity options."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_tool
        from temporalio.contrib.langgraph._tool_registry import clear_registry

        clear_registry()

        @tool
        def my_tool(x: str) -> str:
            """Test tool."""
            return x

        # Should not raise
        wrapped = temporal_tool(
            my_tool,
            start_to_close_timeout=timedelta(minutes=5),
            schedule_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=30),
            task_queue="custom-queue",
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        assert wrapped is not None
        assert wrapped.name == "my_tool"

    def test_wrap_tool_registers_in_registry(self) -> None:
        """temporal_tool should register the tool in the global registry."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_tool
        from temporalio.contrib.langgraph._tool_registry import (
            clear_registry,
            get_tool,
        )

        clear_registry()

        @tool
        def registered_tool(x: str) -> str:
            """A registered tool."""
            return x

        temporal_tool(registered_tool, start_to_close_timeout=timedelta(minutes=1))

        # Original tool should be in registry
        assert get_tool("registered_tool") is registered_tool

    def test_wrapped_tool_runs_directly_outside_workflow(self) -> None:
        """When not in workflow, wrapped tool should execute directly."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_tool
        from temporalio.contrib.langgraph._tool_registry import clear_registry

        clear_registry()

        @tool
        def direct_tool(query: str) -> str:
            """A tool that runs directly."""
            return f"Direct: {query}"

        wrapped = temporal_tool(
            direct_tool,
            start_to_close_timeout=timedelta(minutes=1),
        )

        # Mock workflow.in_workflow to return False
        with patch("temporalio.workflow.in_workflow", return_value=False):
            result = asyncio.get_event_loop().run_until_complete(
                wrapped.ainvoke({"query": "test"})
            )
            assert result == "Direct: test"

    def test_wrapped_tool_executes_as_activity_in_workflow(self) -> None:
        """When in workflow, wrapped tool should execute as activity."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph import temporal_tool
        from temporalio.contrib.langgraph._models import ToolActivityOutput
        from temporalio.contrib.langgraph._tool_registry import clear_registry

        clear_registry()

        @tool
        def activity_tool(query: str) -> str:
            """A tool that runs as activity."""
            return f"Activity: {query}"

        wrapped = temporal_tool(
            activity_tool,
            start_to_close_timeout=timedelta(minutes=1),
        )

        # Mock workflow context
        mock_result = ToolActivityOutput(output="Activity result")

        async def run_test():
            with patch("temporalio.workflow.in_workflow", return_value=True):
                with patch("temporalio.workflow.unsafe.imports_passed_through"):
                    with patch(
                        "temporalio.workflow.execute_activity",
                        new_callable=AsyncMock,
                        return_value=mock_result,
                    ) as mock_execute:
                        result = await wrapped._arun(query="test")

                        # Verify activity was called
                        mock_execute.assert_called_once()
                        call_args = mock_execute.call_args
                        assert call_args[1]["start_to_close_timeout"] == timedelta(
                            minutes=1
                        )

                        assert result == "Activity result"

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_wrap_structured_tool(self) -> None:
        """Should wrap StructuredTool instances."""
        from langchain_core.tools import StructuredTool

        from temporalio.contrib.langgraph import temporal_tool
        from temporalio.contrib.langgraph._tool_registry import clear_registry

        clear_registry()

        def calculator(expression: str) -> float:
            """Calculate a math expression."""
            return eval(expression)

        structured = StructuredTool.from_function(
            calculator,
            name="calculator",
            description="Calculate math expressions",
        )

        wrapped = temporal_tool(
            structured,
            start_to_close_timeout=timedelta(minutes=1),
        )

        assert wrapped.name == "calculator"
        assert "Calculate" in wrapped.description

    def test_wrap_non_tool_raises(self) -> None:
        """Should raise TypeError for non-tool objects."""
        from temporalio.contrib.langgraph import temporal_tool
        from temporalio.contrib.langgraph._tool_registry import clear_registry

        clear_registry()

        with pytest.raises(TypeError, match="Expected BaseTool"):
            temporal_tool(
                "not a tool",  # type: ignore
                start_to_close_timeout=timedelta(minutes=1),
            )


# ==============================================================================
# temporal_model() Tests
# ==============================================================================


class TestTemporalModel:
    """Tests for the temporal_model() wrapper."""

    def test_wrap_model_with_string_name(self) -> None:
        """Should create wrapper from model name string."""
        from temporalio.contrib.langgraph import temporal_model
        from temporalio.contrib.langgraph._model_registry import clear_registry

        clear_registry()

        model = temporal_model(
            "gpt-4o",
            start_to_close_timeout=timedelta(minutes=2),
        )

        assert model is not None
        assert model._llm_type == "temporal-chat-model"

    def test_wrap_model_with_instance(self) -> None:
        """Should wrap a model instance."""
        from temporalio.contrib.langgraph import temporal_model
        from temporalio.contrib.langgraph._model_registry import (
            clear_registry,
            get_model,
        )

        clear_registry()

        # Create a mock model
        mock_base_model = MagicMock()
        mock_base_model.model_name = "mock-model"
        mock_base_model._agenerate = AsyncMock()

        model = temporal_model(
            mock_base_model,
            start_to_close_timeout=timedelta(minutes=2),
        )

        assert model is not None
        # Model instance should be registered
        assert get_model("mock-model") is mock_base_model

    def test_wrap_model_with_all_options(self) -> None:
        """Should accept all activity options."""
        from temporalio.contrib.langgraph import temporal_model
        from temporalio.contrib.langgraph._model_registry import clear_registry

        clear_registry()

        # Should not raise
        model = temporal_model(
            "test-model",
            start_to_close_timeout=timedelta(minutes=5),
            schedule_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=30),
            task_queue="llm-workers",
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        assert model is not None

    def test_wrapped_model_raises_outside_workflow_with_string(self) -> None:
        """When not in workflow with string model, should raise."""
        from langchain_core.messages import HumanMessage

        from temporalio.contrib.langgraph import temporal_model
        from temporalio.contrib.langgraph._model_registry import clear_registry

        clear_registry()

        model = temporal_model(
            "gpt-4o-not-registered",
            start_to_close_timeout=timedelta(minutes=1),
        )

        async def run_test():
            with patch("temporalio.workflow.in_workflow", return_value=False):
                with pytest.raises(RuntimeError, match="Cannot invoke"):
                    await model._agenerate([HumanMessage(content="Hello")])

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_wrapped_model_runs_directly_outside_workflow_with_instance(self) -> None:
        """When not in workflow with model instance, should execute directly."""
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        from temporalio.contrib.langgraph import temporal_model
        from temporalio.contrib.langgraph._model_registry import clear_registry

        clear_registry()

        # Create a mock model that tracks whether _agenerate was called
        call_tracker: dict[str, bool] = {"called": False}

        async def mock_agenerate(messages: Any, **kwargs: Any) -> ChatResult:
            call_tracker["called"] = True
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(content="Hello from model"),
                    )
                ]
            )

        mock_base_model = MagicMock()
        mock_base_model.model_name = "mock-model"
        mock_base_model._agenerate = mock_agenerate

        model = temporal_model(
            mock_base_model,
            start_to_close_timeout=timedelta(minutes=1),
        )

        async def run_test():
            # Patch in the module where it's used
            with patch(
                "temporalio.contrib.langgraph._temporal_model.workflow.in_workflow",
                return_value=False,
            ):
                result = await model._agenerate([HumanMessage(content="Hello")])
                # Verify result content
                assert result.generations[0].message.content == "Hello from model"
                # Verify the underlying model was called
                assert call_tracker["called"], "Expected underlying model._agenerate to be called"

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_wrapped_model_executes_as_activity_in_workflow(self) -> None:
        """When in workflow, wrapped model should execute as activity."""
        from langchain_core.messages import HumanMessage

        from temporalio.contrib.langgraph import temporal_model
        from temporalio.contrib.langgraph._model_registry import clear_registry
        from temporalio.contrib.langgraph._models import ChatModelActivityOutput

        clear_registry()

        model = temporal_model(
            "gpt-4o",
            start_to_close_timeout=timedelta(minutes=2),
        )

        # Mock activity result
        mock_result = ChatModelActivityOutput(
            generations=[
                {
                    "message": {"content": "Activity response", "type": "ai"},
                    "generation_info": None,
                }
            ],
            llm_output=None,
        )

        async def run_test():
            with patch("temporalio.workflow.in_workflow", return_value=True):
                with patch("temporalio.workflow.unsafe.imports_passed_through"):
                    with patch(
                        "temporalio.workflow.execute_activity",
                        new_callable=AsyncMock,
                        return_value=mock_result,
                    ) as mock_execute:
                        result = await model._agenerate([HumanMessage(content="Hello")])

                        # Verify activity was called
                        mock_execute.assert_called_once()
                        call_args = mock_execute.call_args
                        assert call_args[1]["start_to_close_timeout"] == timedelta(
                            minutes=2
                        )

                        # Result should be reconstructed
                        assert len(result.generations) == 1
                        assert result.generations[0].message.content == "Activity response"

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_bind_tools_raises_not_implemented(self) -> None:
        """bind_tools should raise NotImplementedError."""
        from temporalio.contrib.langgraph import temporal_model
        from temporalio.contrib.langgraph._model_registry import clear_registry

        clear_registry()

        model = temporal_model(
            "gpt-4o",
            start_to_close_timeout=timedelta(minutes=1),
        )

        with pytest.raises(NotImplementedError, match="Tool binding"):
            model.bind_tools([])


# ==============================================================================
# Activity Tests
# ==============================================================================


class TestToolActivity:
    """Tests for the execute_tool activity."""

    def test_execute_tool_activity(self) -> None:
        """execute_tool should execute registered tool and return output."""
        from langchain_core.tools import tool

        from temporalio.contrib.langgraph._activities import execute_tool
        from temporalio.contrib.langgraph._models import ToolActivityInput
        from temporalio.contrib.langgraph._tool_registry import (
            clear_registry,
            register_tool,
        )

        clear_registry()

        @tool
        def greeting_tool(name: str) -> str:
            """Greet someone."""
            return f"Hello, {name}!"

        register_tool(greeting_tool)

        input_data = ToolActivityInput(
            tool_name="greeting_tool",
            tool_input={"name": "World"},
        )

        result = asyncio.get_event_loop().run_until_complete(
            execute_tool(input_data)
        )

        assert result.output == "Hello, World!"

    def test_execute_tool_activity_not_found(self) -> None:
        """execute_tool should raise KeyError for unregistered tool."""
        from temporalio.contrib.langgraph._activities import execute_tool
        from temporalio.contrib.langgraph._models import ToolActivityInput
        from temporalio.contrib.langgraph._tool_registry import clear_registry

        clear_registry()

        input_data = ToolActivityInput(
            tool_name="nonexistent_tool",
            tool_input={"x": 1},
        )

        with pytest.raises(KeyError, match="not found"):
            asyncio.get_event_loop().run_until_complete(
                execute_tool(input_data)
            )


class TestChatModelActivity:
    """Tests for the execute_chat_model activity."""

    def test_execute_chat_model_activity(self) -> None:
        """execute_chat_model should execute registered model."""
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        from temporalio.contrib.langgraph._activities import execute_chat_model
        from temporalio.contrib.langgraph._model_registry import (
            clear_registry,
            register_model,
        )
        from temporalio.contrib.langgraph._models import ChatModelActivityInput

        clear_registry()

        # Create and register a mock model with real AIMessage
        mock_result = ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content="Model response"),
                    generation_info={"finish_reason": "stop"},
                )
            ],
            llm_output={"model": "test"},
        )

        mock_model = MagicMock()
        mock_model.model_name = "test-model"
        mock_model._agenerate = AsyncMock(return_value=mock_result)

        register_model(mock_model)

        input_data = ChatModelActivityInput(
            model_name="test-model",
            messages=[{"content": "Hello", "type": "human"}],
            stop=None,
            kwargs={},
        )

        result = asyncio.get_event_loop().run_until_complete(
            execute_chat_model(input_data)
        )

        assert len(result.generations) == 1
        assert result.generations[0]["message"]["content"] == "Model response"
        assert result.llm_output == {"model": "test"}

    def test_execute_chat_model_not_found(self) -> None:
        """execute_chat_model should raise KeyError for unregistered model."""
        from temporalio.contrib.langgraph._activities import execute_chat_model
        from temporalio.contrib.langgraph._model_registry import clear_registry
        from temporalio.contrib.langgraph._models import ChatModelActivityInput

        clear_registry()

        input_data = ChatModelActivityInput(
            model_name="nonexistent-model",
            messages=[{"content": "Hello", "type": "human"}],
            stop=None,
            kwargs={},
        )

        with pytest.raises(KeyError, match="not found"):
            asyncio.get_event_loop().run_until_complete(
                execute_chat_model(input_data)
            )


# ==============================================================================
# Plugin Registration Tests
# ==============================================================================


class TestPluginRegistersActivities:
    """Tests that plugin registers tool/model activities."""

    def test_plugin_registers_tool_and_model_activities(self) -> None:
        """LangGraphPlugin should register execute_tool and execute_chat_model."""
        from temporalio.contrib.langgraph import LangGraphPlugin
        from temporalio.contrib.langgraph._activities import (
            execute_chat_model,
            execute_node,
            execute_tool,
        )
        from temporalio.contrib.langgraph._graph_registry import get_global_registry

        get_global_registry().clear()

        # Create plugin
        plugin = LangGraphPlugin(graphs={})

        # The plugin modifies activities via a transformer callable
        # When called with an empty list, it should add the langgraph activities
        assert callable(plugin.activities)
        activities = plugin.activities([])  # type: ignore[misc]

        # Should include execute_node, execute_tool, and execute_chat_model
        assert execute_node in activities
        assert execute_tool in activities
        assert execute_chat_model in activities


# ==============================================================================
# Model Input/Output Tests
# ==============================================================================


class TestActivityModels:
    """Tests for activity input/output models."""

    def test_tool_activity_input(self) -> None:
        """ToolActivityInput should store tool name and input."""
        from temporalio.contrib.langgraph._models import ToolActivityInput

        input_data = ToolActivityInput(
            tool_name="my_tool",
            tool_input={"query": "test"},
        )

        assert input_data.tool_name == "my_tool"
        assert input_data.tool_input == {"query": "test"}

    def test_tool_activity_output(self) -> None:
        """ToolActivityOutput should store output."""
        from temporalio.contrib.langgraph._models import ToolActivityOutput

        output = ToolActivityOutput(output="result")
        assert output.output == "result"

    def test_chat_model_activity_input(self) -> None:
        """ChatModelActivityInput should store model info and messages."""
        from temporalio.contrib.langgraph._models import ChatModelActivityInput

        input_data = ChatModelActivityInput(
            model_name="gpt-4o",
            messages=[
                {"content": "Hello", "type": "human"},
                {"content": "Hi there!", "type": "ai"},
            ],
            stop=["END"],
            kwargs={"temperature": 0.7},
        )

        assert input_data.model_name == "gpt-4o"
        assert len(input_data.messages) == 2
        assert input_data.stop == ["END"]
        assert input_data.kwargs == {"temperature": 0.7}

    def test_chat_model_activity_output(self) -> None:
        """ChatModelActivityOutput should store generations."""
        from temporalio.contrib.langgraph._models import ChatModelActivityOutput

        output = ChatModelActivityOutput(
            generations=[
                {
                    "message": {"content": "Response", "type": "ai"},
                    "generation_info": {"finish_reason": "stop"},
                }
            ],
            llm_output={"usage": {"tokens": 100}},
        )

        assert len(output.generations) == 1
        assert output.generations[0]["message"]["content"] == "Response"
        assert output.llm_output == {"usage": {"tokens": 100}}


# ==============================================================================
# End-to-End Tests with React Agent
# ==============================================================================

# Module-level definitions for e2e tests (required for Temporal)

import uuid
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.langgraph import (
    LangGraphPlugin,
    compile as lg_compile,
    temporal_tool,
)


# Define tools at module level for registry
@pytest.fixture(scope="module", autouse=True)
def setup_react_agent_tools():
    """Set up tools for react agent tests."""
    from langchain_core.tools import tool

    from temporalio.contrib.langgraph._tool_registry import clear_registry

    clear_registry()

    @tool
    def calculator(expression: str) -> str:
        """Calculate a math expression. Input should be a valid Python math expression."""
        try:
            result = eval(expression)  # Safe in test context
            return f"Result: {result}"
        except Exception as e:
            return f"Error: {e}"

    @tool
    def get_weather(location: str) -> str:
        """Get the weather for a location."""
        # Fake weather data for testing
        weather_data = {
            "san francisco": "65°F, foggy",
            "new york": "72°F, sunny",
            "london": "55°F, rainy",
        }
        return weather_data.get(location.lower(), "Weather data not available")

    return {"calculator": calculator, "get_weather": get_weather}


class FakeToolCallingModel:
    """A fake chat model that simulates tool calling behavior for testing.

    This model follows a simple script:
    1. First call: Returns a tool call for calculator
    2. After receiving tool result: Returns final answer

    Note: This is created dynamically in build_react_agent_graph to properly
    inherit from BaseChatModel which requires LangChain imports.
    """

    pass  # Placeholder - actual implementation in build_react_agent_graph


def build_react_agent_graph():
    """Build a react agent graph with temporal tools for e2e testing."""
    from typing import List, Optional

    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import tool
    from langgraph.prebuilt import create_react_agent

    from temporalio.contrib.langgraph import temporal_tool
    from temporalio.contrib.langgraph._tool_registry import clear_registry

    clear_registry()

    # Create a proper fake model that inherits from BaseChatModel
    class _FakeToolCallingModel(BaseChatModel):
        """Fake model that simulates tool calling for testing."""

        @property
        def _llm_type(self) -> str:
            return "fake-tool-model"

        def _generate(
            self,
            messages: List[BaseMessage],
            stop: Optional[List[str]] = None,
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
        ) -> "_FakeToolCallingModel":
            """Return self - tools are handled in _generate."""
            return self

    # Create tools
    @tool
    def calculator(expression: str) -> str:
        """Calculate a math expression. Input should be a valid Python math expression."""
        try:
            result = eval(expression)
            return f"Result: {result}"
        except Exception as e:
            return f"Error: {e}"

    # Wrap tool with temporal_tool for durable execution
    durable_calculator = temporal_tool(
        calculator,
        start_to_close_timeout=timedelta(seconds=30),
    )

    # Create fake model
    model = _FakeToolCallingModel()

    # Create react agent
    agent = create_react_agent(model, [durable_calculator])

    return agent


@workflow.defn(sandboxed=False)
class ReactAgentWorkflow:
    """Workflow that runs a react agent with temporal tools."""

    @workflow.run
    async def run(self, question: str) -> dict[str, Any]:
        """Run the react agent and return the result."""
        from langchain_core.messages import HumanMessage

        app = lg_compile("react_agent_test")

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


class TestReactAgentE2E:
    """End-to-end tests for react agent with temporal_tool."""

    @pytest.mark.asyncio
    async def test_react_agent_with_temporal_tool(self, client: Client) -> None:
        """Test react agent using temporal_tool for durable tool execution."""
        from temporalio.contrib.langgraph._graph_registry import get_global_registry
        from tests.helpers import new_worker

        # Clear registry
        get_global_registry().clear()

        # Create plugin with the react agent graph
        plugin = LangGraphPlugin(
            graphs={"react_agent_test": build_react_agent_graph},
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
            ReactAgentWorkflow,
        ) as worker:
            result = await plugin_client.execute_workflow(
                ReactAgentWorkflow.run,
                "What is 2 + 2?",
                id=f"react-agent-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=60),
            )

            # Verify the agent produced a result
            assert result["message_count"] >= 3  # Human, AI (tool call), Tool, AI (answer)
            assert "4" in result["answer"]  # Should contain the calculation result
