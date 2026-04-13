# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Integration tests for ADK Temporal support."""

import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any

import pytest
from google.adk import Agent, Runner
from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from google.genai.types import Content, FunctionCall, Part
from mcp import StdioServerParameters
from openinference.instrumentation.google_adk import GoogleADKInstrumentor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import set_tracer_provider

import temporalio.contrib.google_adk_agents.workflow
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.google_adk_agents import (
    GoogleAdkPlugin,
    TemporalMcpToolSet,
    TemporalMcpToolSetProvider,
    TemporalModel,
)
from temporalio.contrib.opentelemetry import OpenTelemetryPlugin, create_tracer_provider
from temporalio.worker import Worker
from temporalio.workflow import ActivityConfig
from tests.contrib.opentelemetry.test_opentelemetry import dump_spans

logger = logging.getLogger(__name__)


@activity.defn
async def get_weather(city: str) -> str:  #  type: ignore[reportUnusedParameter]
    """Activity that gets weather for a given city."""
    return "Warm and sunny. 17 degrees."


def weather_agent(model_name: str) -> Agent:
    # Wraps 'get_weather' activity as a Tool
    weather_tool = temporalio.contrib.google_adk_agents.workflow.activity_tool(
        get_weather, start_to_close_timeout=timedelta(seconds=60)
    )

    return Agent(
        name="test_agent",
        model=TemporalModel(model_name),
        tools=[weather_tool],
    )


@workflow.defn
class WeatherAgent:
    @workflow.run
    async def run(self, prompt: str, model_name: str) -> Event | None:
        logger.info("Workflow started.")

        # 1. Define Agent using Temporal Helpers
        # Note: AgentPlugin in the Runner automatically handles Runtime setup
        # and Model Activity interception. We use standard ADK models now.
        agent = weather_agent(model_name)

        # 2. Create runner
        runner = InMemoryRunner(
            agent=agent,
            app_name="test_app",
        )

        # 3. Create Session (uses runtime.new_uuid() -> workflow.uuid4())
        logger.info("Create session.")
        session = await runner.session_service.create_session(
            app_name="test_app", user_id="test"
        )
        logger.info(f"Session created with ID: {session.id}")

        # 4. Run
        logger.info("Starting runner.")
        last_event = None
        async with Aclosing(
            runner.run_async(
                user_id="test",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
            )
        ) as agen:
            async for event in agen:
                logger.info(f"Event: {event}")
                last_event = event

        return last_event


@workflow.defn
class MultiAgentWorkflow:
    @workflow.run
    async def run(self, topic: str, model_name: str) -> str | None:
        # 1. Setup Session Service
        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="multi_agent_app", user_id="test_user"
        )

        # 2. Define Agents
        # Sub-agent: Researcher
        researcher = LlmAgent(
            name="researcher",
            model=TemporalModel(
                model_name, activity_config=ActivityConfig(summary="Researcher Agent")
            ),
            instruction="You are a researcher. Find information about the topic.",
        )

        # Sub-agent: Writer
        writer = LlmAgent(
            name="writer",
            model=TemporalModel(
                model_name, activity_config=ActivityConfig(summary="Writer Agent")
            ),
            instruction="You are a poet. Write a haiku based on the research.",
        )

        # Root Agent: Coordinator
        coordinator = LlmAgent(
            name="coordinator",
            model=TemporalModel(
                model_name,
                activity_config=ActivityConfig(
                    start_to_close_timeout=timedelta(seconds=30),
                    summary="Coordinator Agent",
                ),
            ),
            instruction="You are a coordinator. Delegate to researcher then writer.",
            sub_agents=[researcher, writer],
        )

        # 3. Initialize Runner with required args
        runner = Runner(
            agent=coordinator,
            app_name="multi_agent_app",
            session_service=session_service,
        )

        # 4. Run
        final_content = ""
        user_msg = types.Content(
            role="user",
            parts=[
                types.Part(
                    text=f"Write a haiku about {topic}. First research it, then write it."
                )
            ],
        )
        async for event in runner.run_async(
            user_id="test_user", session_id=session.id, new_message=user_msg
        ):
            if (
                event.content
                and event.content.parts
                and event.content.parts[0].text is not None
            ):
                final_content = event.content.parts[0].text

        return final_content


class TestModel(BaseLlm, ABC):
    @abstractmethod
    def responses(self) -> list[LlmResponse]:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def supported_models(cls) -> list[str]:
        raise NotImplementedError

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        for response in self.responses():
            if any(content == response.content for content in llm_request.contents):
                continue
            yield response
            return


class WeatherModel(TestModel):
    def responses(self) -> list[LlmResponse]:
        return [
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[
                        Part(
                            function_call=FunctionCall(
                                args={"city": "New York"}, name="get_weather"
                            )
                        )
                    ],
                )
            ),
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[Part(text="warm and sunny")],
                )
            ),
        ]

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["weather_model"]


@pytest.mark.parametrize("use_local_model", [True, False])
@pytest.mark.asyncio
async def test_single_agent(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("No google API key")

    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    # Run Worker with the ADK plugin
    async with Worker(
        client,
        task_queue="adk-task-queue",
        activities=[
            get_weather,
        ],
        workflows=[WeatherAgent],
        max_cached_workflows=0,
    ):
        if use_local_model:
            LLMRegistry.register(WeatherModel)

        # Test Weather Agent
        handle = await client.start_workflow(
            WeatherAgent.run,
            args=[
                "What is the weather in New York?",
                "weather_model" if use_local_model else "gemini-2.5-pro",
            ],
            id=f"weather-agent-workflow-{uuid.uuid4()}",
            task_queue="adk-task-queue",
            execution_timeout=timedelta(seconds=60),
        )
        result = await handle.result()
        print(f"Workflow result: {result}")
        if use_local_model:
            assert result is not None
            assert result.content is not None
            assert result.content.parts is not None
            assert result.content.parts[0].text == "warm and sunny"


class ResearchModel(TestModel):
    def responses(self) -> list[LlmResponse]:
        return [
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[
                        Part(
                            function_call=FunctionCall(
                                args={"agent_name": "researcher"},
                                name="transfer_to_agent",
                            )
                        )
                    ],
                )
            ),
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[
                        Part(
                            function_call=FunctionCall(
                                args={"agent_name": "writer"}, name="transfer_to_agent"
                            )
                        )
                    ],
                )
            ),
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[Part(text="haiku")],
                )
            ),
        ]

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["research_model"]


@pytest.mark.parametrize("use_local_model", [True, False])
@pytest.mark.asyncio
async def test_multi_agent(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("No google API key")

    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    # Run Worker with the ADK plugin
    async with Worker(
        client,
        task_queue="adk-task-queue-multi-agent",
        workflows=[MultiAgentWorkflow],
        max_cached_workflows=0,
    ):
        if use_local_model:
            LLMRegistry.register(ResearchModel)

        # Test Multi Agent
        handle = await client.start_workflow(
            MultiAgentWorkflow.run,
            args=[
                "Run mult-agent flow",
                "research_model" if use_local_model else "gemini-2.5-pro",
            ],
            id=f"multi-agent-workflow-{uuid.uuid4()}",
            task_queue="adk-task-queue-multi-agent",
            execution_timeout=timedelta(seconds=60),
        )
        result = await handle.result()
        print(f"Multi-Agent Workflow result: {result}")
        if use_local_model:
            assert result == "haiku"


def example_toolset(_: Any | None) -> McpToolset:
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="npx",
                args=[
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    os.path.dirname(os.path.abspath(__file__)),
                ],
            ),
        ),
    )


def mcp_agent(model_name: str) -> Agent:
    return Agent(
        name="test_agent",
        # instruction="Always use your tools to answer questions.",
        model=TemporalModel(model_name),
        tools=[TemporalMcpToolSet("test_set", not_in_workflow_toolset=example_toolset)],
    )


@workflow.defn
class McpAgent:
    @workflow.run
    async def run(self, prompt: str, model_name: str) -> str:
        logger.info("Workflow started.")

        # 1. Define Agent using Temporal Helpers
        agent = mcp_agent(model_name)

        # 2. Create Session (uses runtime.new_uuid() -> workflow.uuid4())
        session_service = InMemorySessionService()
        logger.info("Create session.")
        session = await session_service.create_session(
            app_name="test_app", user_id="test"
        )

        logger.info(f"Session created with ID: {session.id}")

        # 3. Run Agent with AgentPlugin
        runner = Runner(
            agent=agent,
            app_name="test_app",
            session_service=session_service,
        )

        last_event = None
        async with Aclosing(
            runner.run_async(
                user_id="test",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
            )
        ) as agen:
            async for event in agen:
                logger.info(f"Event: {event}")
                last_event = event

        assert last_event
        assert last_event.content
        assert last_event.content.parts
        assert last_event.content.parts[0].text
        return last_event.content.parts[0].text


class McpModel(TestModel):
    def responses(self) -> list[LlmResponse]:
        return [
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[
                        Part(
                            function_call=FunctionCall(
                                args={
                                    "path": os.path.dirname(os.path.abspath(__file__))
                                },
                                name="list_directory",
                            )
                        )
                    ],
                )
            ),
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[Part(text="Some files.")],
                )
            ),
        ]

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["mcp_model"]


@pytest.mark.parametrize("use_local_model", [True, False])
@pytest.mark.asyncio
@pytest.mark.skip  # Doesn't work well in CI currently
async def test_mcp_agent(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("No google API key")

    new_config = client.config()
    new_config["plugins"] = [
        GoogleAdkPlugin(
            toolset_providers=[
                TemporalMcpToolSetProvider(
                    "test_set",
                    example_toolset,
                )
            ],
        )
    ]
    client = Client(**new_config)

    # Run Worker with the ADK plugin
    async with Worker(
        client,
        task_queue="adk-task-queue-mcp",
        workflows=[McpAgent],
        max_cached_workflows=0,
    ):
        if use_local_model:
            LLMRegistry.register(McpModel)

        # Test Multi Agent
        handle = await client.start_workflow(
            McpAgent.run,
            args=[
                "What files are in the current directory?",
                "mcp_model" if use_local_model else "gemini-2.5-pro",
            ],
            id=f"mcp-agent-workflow-{uuid.uuid4()}",
            task_queue="adk-task-queue-mcp",
            execution_timeout=timedelta(seconds=60),
        )
        result = await handle.result()
        print(f"MCP-Agent Workflow result: {result}")
        if use_local_model:
            assert result == "Some files."


@pytest.mark.asyncio
async def test_single_agent_telemetry(
    client: Client,
    reset_otel_tracer_provider,  # type: ignore[reportUnusedParameter]
):
    exporter = InMemorySpanExporter()
    provider = create_tracer_provider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    set_tracer_provider(provider)
    GoogleADKInstrumentor().instrument()

    new_config = client.config()
    new_config["plugins"] = [
        GoogleAdkPlugin(),
        OpenTelemetryPlugin(add_temporal_spans=True),
    ]
    client = Client(**new_config)

    # Run Worker with the ADK plugin
    async with Worker(
        client,
        task_queue="adk-task-queue-telemetry",
        activities=[
            get_weather,
        ],
        workflows=[WeatherAgent],
        max_cached_workflows=0,
    ):
        LLMRegistry.register(WeatherModel)

        # Test Weather Agent
        handle = await client.start_workflow(
            WeatherAgent.run,
            args=[
                "What is the weather in New York?",
                "weather_model",
            ],
            id=f"weather-agent-telemetry-workflow-{uuid.uuid4()}",
            task_queue="adk-task-queue-telemetry",
            execution_timeout=timedelta(seconds=60),
        )
        result = await handle.result()
        print(f"Workflow result: {result}")

        assert result is not None
        assert result.content is not None
        assert result.content.parts is not None
        assert result.content.parts[0].text == "warm and sunny"

    print("\n".join(dump_spans(exporter.get_finished_spans(), with_attributes=False)))
    assert dump_spans(exporter.get_finished_spans(), with_attributes=False) == [
        "StartWorkflow:WeatherAgent",
        "  RunWorkflow:WeatherAgent",
        "    invocation [test_app]",
        "      agent_run [test_agent]",
        "        call_llm",
        "          StartActivity:invoke_model",
        "            RunActivity:invoke_model",
        "          execute_tool get_weather",
        "            StartActivity:get_weather",
        "              RunActivity:get_weather",
        "        call_llm",
        "          StartActivity:invoke_model",
        "            RunActivity:invoke_model",
    ]


async def test_unsetting_timeout():
    model = TemporalModel("", ActivityConfig(start_to_close_timeout=None))
    assert model._activity_config.get("start_to_close_timeout", None) is None


@pytest.mark.asyncio
async def test_agent_outside_workflow():
    """Test that an agent using TemporalModel and activity_tool works outside a Temporal workflow."""
    LLMRegistry.register(WeatherModel)

    agent = weather_agent("weather_model")

    runner = InMemoryRunner(
        agent=agent,
        app_name="test_app_local",
    )

    session = await runner.session_service.create_session(
        app_name="test_app_local", user_id="test"
    )

    last_event = None
    async with Aclosing(
        runner.run_async(
            user_id="test",
            session_id=session.id,
            new_message=types.Content(
                role="user", parts=[types.Part(text="What is the weather in New York?")]
            ),
        )
    ) as agen:
        async for event in agen:
            last_event = event

    assert last_event is not None
    assert last_event.content is not None
    assert last_event.content.parts is not None
    assert last_event.content.parts[0].text == "warm and sunny"


@pytest.mark.asyncio
@pytest.mark.skip  # Doesn't work well in CI currently
async def test_mcp_agent_outside_workflow():
    """Test that an agent using TemporalMcpToolSet works outside a Temporal workflow."""
    LLMRegistry.register(McpModel)

    agent = mcp_agent("mcp_model")

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="test_app_local", user_id="test"
    )

    runner = Runner(
        agent=agent,
        app_name="test_app_local",
        session_service=session_service,
    )

    last_event = None
    async with Aclosing(
        runner.run_async(
            user_id="test",
            session_id=session.id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text="What files are in the current directory?")],
            ),
        )
    ) as agen:
        async for event in agen:
            last_event = event

    assert last_event is not None
    assert last_event.content is not None
    assert last_event.content.parts is not None
    assert last_event.content.parts[0].text == "Some files."


@pytest.mark.asyncio
async def test_mcp_toolset_outside_workflow_no_not_in_workflow_toolset():
    """Test that TemporalMcpToolSet raises ValueError outside a workflow with no not_in_workflow_toolset."""
    toolset = TemporalMcpToolSet("test_set_no_local")
    with pytest.raises(
        ValueError,
        match="not_in_workflow_toolset",
    ):
        await toolset.get_tools()


complex_activity_inputs_seen: dict[str, object] = {}


@activity.defn
async def book_trip(origin: str, destination: str, passengers: int) -> str:
    """Activity that formats multiple discrete arguments."""
    complex_activity_inputs_seen["book_trip"] = (origin, destination, passengers)
    return f"{origin}->{destination}:{passengers}"


@activity.defn
async def summarize_payload(
    name: str, metadata: dict[str, str | int | list[str]]
) -> str:
    """Activity that formats compound map input."""
    complex_activity_inputs_seen["summarize_payload"] = (name, metadata)
    tags = metadata.get("tags", [])
    assert isinstance(tags, list)
    return f"{name}:{metadata['count']}:{metadata['owner']}:" + ",".join(
        str(tag) for tag in tags
    )


class ComplexActivityMethodHolder:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    @activity.defn
    async def annotate_trip(self, trip: str) -> str:
        complex_activity_inputs_seen["annotate_trip"] = trip
        return f"{self.prefix}:{trip}"


@workflow.defn
class ComplexActivityInputAgent:
    @workflow.run
    async def run(self, prompt: str, model_name: str) -> str:
        logger.info("Workflow started.")
        method_holder = ComplexActivityMethodHolder("method")

        agent = Agent(
            name="complex_input_agent",
            model=TemporalModel(model_name),
            tools=[
                temporalio.contrib.google_adk_agents.workflow.activity_tool(
                    book_trip, start_to_close_timeout=timedelta(seconds=60)
                ),
                temporalio.contrib.google_adk_agents.workflow.activity_tool(
                    summarize_payload, start_to_close_timeout=timedelta(seconds=60)
                ),
                temporalio.contrib.google_adk_agents.workflow.activity_tool(
                    method_holder.annotate_trip,
                    start_to_close_timeout=timedelta(seconds=60),
                ),
            ],
        )

        runner = InMemoryRunner(
            agent=agent,
            app_name="complex_input_app",
        )

        session = await runner.session_service.create_session(
            app_name="complex_input_app", user_id="test"
        )

        final_text = ""
        async with Aclosing(
            runner.run_async(
                user_id="test",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
            )
        ) as agen:
            async for event in agen:
                logger.info(f"Event: {event}")
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text is not None:
                            final_text = part.text

        return final_text


class ComplexActivityInputModel(TestModel):
    def responses(self) -> list[LlmResponse]:
        return [
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[
                        Part(
                            function_call=FunctionCall(
                                name="book_trip",
                                args={
                                    "origin": "SFO",
                                    "destination": "LAX",
                                    "passengers": 3,
                                },
                            )
                        )
                    ],
                )
            ),
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[
                        Part(
                            function_call=FunctionCall(
                                name="summarize_payload",
                                args={
                                    "name": "fixture",
                                    "metadata": {
                                        "count": 2,
                                        "owner": "team-a",
                                        "tags": ["alpha", "beta"],
                                    },
                                },
                            )
                        )
                    ],
                )
            ),
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[
                        Part(
                            function_call=FunctionCall(
                                name="annotate_trip",
                                args={"trip": "SFO->LAX:3"},
                            )
                        )
                    ],
                )
            ),
            LlmResponse(
                content=Content(
                    role="model",
                    parts=[Part(text="completed complex input tool calls")],
                )
            ),
        ]

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["complex_activity_input_model"]


@pytest.mark.asyncio
async def test_activity_tool_supports_complex_inputs_via_adk(client: Client):
    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)
    complex_activity_inputs_seen.clear()
    method_holder = ComplexActivityMethodHolder("method")

    async with Worker(
        client,
        task_queue="adk-task-queue-complex-inputs",
        activities=[
            book_trip,
            summarize_payload,
            method_holder.annotate_trip,
        ],
        workflows=[ComplexActivityInputAgent],
        max_cached_workflows=0,
    ):
        LLMRegistry.register(ComplexActivityInputModel)

        handle = await client.start_workflow(
            ComplexActivityInputAgent.run,
            args=[
                "Run every registered tool using structured inputs.",
                "complex_activity_input_model",
            ],
            id=f"complex-activity-input-workflow-{uuid.uuid4()}",
            task_queue="adk-task-queue-complex-inputs",
            execution_timeout=timedelta(seconds=60),
        )
        result = await handle.result()
        assert result == "completed complex input tool calls"
        assert complex_activity_inputs_seen == {
            "book_trip": ("SFO", "LAX", 3),
            "summarize_payload": (
                "fixture",
                {"count": 2, "owner": "team-a", "tags": ["alpha", "beta"]},
            ),
            "annotate_trip": "SFO->LAX:3",
        }


def litellm_agent(model_name: str) -> Agent:
    return Agent(
        name="litellm_test_agent",
        model=TemporalModel(model_name),
    )


@workflow.defn
class LiteLlmWorkflow:
    @workflow.run
    async def run(self, prompt: str, model_name: str) -> Event | None:
        agent = litellm_agent(model_name)

        runner = InMemoryRunner(
            agent=agent,
            app_name="litellm_test_app",
        )

        session = await runner.session_service.create_session(
            app_name="litellm_test_app", user_id="test"
        )

        last_event = None
        async with Aclosing(
            runner.run_async(
                user_id="test",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
            )
        ) as agen:
            async for event in agen:
                last_event = event

        return last_event


@pytest.mark.asyncio
async def test_litellm_model(client: Client):
    """Test that a litellm-backed model works with TemporalModel through a full Temporal workflow."""
    import litellm as litellm_module
    from google.adk.models.lite_llm import LiteLlm
    from litellm import ModelResponse
    from litellm.llms.custom_llm import CustomLLM

    class FakeLiteLlmProvider(CustomLLM):
        """A fake litellm provider that returns canned responses locally."""

        def _make_response(self, model: str) -> ModelResponse:
            return ModelResponse(
                choices=[
                    {
                        "message": {
                            "content": "hello from litellm",
                            "role": "assistant",
                        },
                        "index": 0,
                        "finish_reason": "stop",
                    }
                ],
                model=model,
            )

        def completion(self, *args: Any, **kwargs: Any) -> ModelResponse:
            model = args[0] if args else kwargs.get("model", "unknown")
            return self._make_response(model)

        async def acompletion(self, *args: Any, **kwargs: Any) -> ModelResponse:
            return self.completion(*args, **kwargs)

    class FakeLiteLlm(LiteLlm):
        """LiteLlm subclass that supports the fake/test-model name for testing."""

        @classmethod
        def supported_models(cls) -> list[str]:
            return ["fake/test-model"]

    # Register our fake provider with litellm
    litellm_module.custom_provider_map = [
        {"provider": "fake", "custom_handler": FakeLiteLlmProvider()}
    ]

    LLMRegistry.register(FakeLiteLlm)

    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    async with Worker(
        client,
        task_queue="adk-task-queue-litellm",
        workflows=[LiteLlmWorkflow],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            LiteLlmWorkflow.run,
            args=["Say hello", "fake/test-model"],
            id=f"litellm-agent-workflow-{uuid.uuid4()}",
            task_queue="adk-task-queue-litellm",
            execution_timeout=timedelta(seconds=60),
        )
        result = await handle.result()

    assert result is not None
    assert result.content is not None
    assert result.content.parts is not None
    assert result.content.parts[0].text == "hello from litellm"


def test_unset_none_fields_stripped() -> None:
    """ADK plugin converter strips unset None fields from Pydantic payloads."""
    plugin = GoogleAdkPlugin()
    converter = plugin._configure_data_converter(None)
    request = LlmRequest(
        model="gemini-2.0-flash",
        contents=[Content(parts=[Part(text="hello")])],
    )
    payloads = converter.payload_converter.to_payloads([request])
    serialized = json.loads(payloads[0].data)

    assert serialized["model"] == "gemini-2.0-flash"
    assert "contents" in serialized
    for field in (
        "cache_config",
        "cache_metadata",
        "cacheable_contents_token_count",
        "previous_interaction_id",
    ):
        assert field not in serialized, f"Unset field {field!r} should be stripped"


def test_explicitly_set_none_preserved() -> None:
    """Explicitly-set None is preserved (exclude_unset, not exclude_none)."""
    plugin = GoogleAdkPlugin()
    converter = plugin._configure_data_converter(None)
    request = LlmRequest(
        model="gemini-2.0-flash",
        contents=[Content(parts=[Part(text="hello")])],
        cache_config=None,
    )
    payloads = converter.payload_converter.to_payloads([request])
    serialized = json.loads(payloads[0].data)

    assert "cache_config" in serialized, "Explicitly-set None should be preserved"
    assert serialized["cache_config"] is None
