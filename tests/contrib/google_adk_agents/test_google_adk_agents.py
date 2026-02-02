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

import logging
import os
import uuid
from collections.abc import AsyncGenerator, Iterator
from datetime import timedelta

import pytest
from google.adk import Agent, Runner
from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from google.genai.types import Content, FunctionCall, Part
from mcp import StdioServerParameters
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.google_adk_agents import (
    AdkAgentPlugin,
    TemporalAdkPlugin,
    TemporalMcpToolSet,
    TemporalMcpToolSetProvider,
)
from temporalio.worker import Worker

logger = logging.getLogger(__name__)


@activity.defn
async def get_weather(city: str) -> str:  #  type: ignore[reportUnusedParameter]
    """Activity that gets weather for a given city."""
    return "Warm and sunny. 17 degrees."


@workflow.defn
class WeatherAgent:
    @workflow.run
    async def run(self, prompt: str, model_name: str) -> Event | None:
        logger.info("Workflow started.")

        # 1. Define Agent using Temporal Helpers
        # Note: AgentPlugin in the Runner automatically handles Runtime setup
        # and Model Activity interception. We use standard ADK models now.

        # Wraps 'get_weather' activity as a Tool
        weather_tool = AdkAgentPlugin.activity_tool(
            get_weather, start_to_close_timeout=timedelta(seconds=60)
        )

        agent = Agent(
            name="test_agent",
            model=model_name,
            tools=[weather_tool],
        )

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
            plugins=[
                AdkAgentPlugin(
                    activity_options={"start_to_close_timeout": timedelta(minutes=2)}
                )
            ],
        )

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
            model=model_name,
            instruction="You are a researcher. Find information about the topic.",
        )

        # Sub-agent: Writer
        writer = LlmAgent(
            name="writer",
            model=model_name,
            instruction="You are a poet. Write a haiku based on the research.",
        )

        # Root Agent: Coordinator
        coordinator = LlmAgent(
            name="coordinator",
            model=model_name,
            instruction="You are a coordinator. Delegate to researcher then writer.",
            sub_agents=[researcher, writer],
        )

        # 3. Initialize Runner with required args
        runner = Runner(
            agent=coordinator,
            app_name="multi_agent_app",
            session_service=session_service,
            plugins=[
                AdkAgentPlugin(
                    activity_options={"start_to_close_timeout": timedelta(minutes=2)}
                )
            ],
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


class WeatherModel(BaseLlm):
    responses: list[LlmResponse] = [
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
    response_iter: Iterator[LlmResponse] = iter(responses)

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["weather_model"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield next(self.response_iter)


@pytest.mark.parametrize("use_local_model", [True, False])
@pytest.mark.asyncio
async def test_single_agent(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("No google API key")

    new_config = client.config()
    new_config["plugins"] = [TemporalAdkPlugin()]
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
        )
        result = await handle.result()
        print(f"Workflow result: {result}")
        if use_local_model:
            assert result is not None
            assert result.content is not None
            assert result.content.parts is not None
            assert result.content.parts[0].text == "warm and sunny"


class ResearchModel(BaseLlm):
    responses: list[LlmResponse] = [
        LlmResponse(
            content=Content(
                role="model",
                parts=[
                    Part(
                        function_call=FunctionCall(
                            args={"agent_name": "researcher"}, name="transfer_to_agent"
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
    response_iter: Iterator[LlmResponse] = iter(responses)

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["research_model"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield next(self.response_iter)


@pytest.mark.parametrize("use_local_model", [True, False])
@pytest.mark.asyncio
async def test_multi_agent(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("No google API key")

    new_config = client.config()
    new_config["plugins"] = [TemporalAdkPlugin()]
    client = Client(**new_config)

    # Run Worker with the ADK plugin
    async with Worker(
        client,
        task_queue="adk-task-queue",
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
            task_queue="adk-task-queue",
        )
        result = await handle.result()
        print(f"Multi-Agent Workflow result: {result}")
        if use_local_model:
            assert result == "haiku"


@workflow.defn
class McpAgent:
    @workflow.run
    async def run(self, prompt: str, model_name: str) -> Event | None:
        logger.info("Workflow started.")

        # 1. Define Agent using Temporal Helpers
        # Note: AgentPlugin in the Runner automatically handles Runtime setup
        # and Model Activity interception. We use standard ADK models now.
        agent = Agent(
            name="test_agent",
            # instruction="Always use your tools to answer questions.",
            model=model_name,
            tools=[TemporalMcpToolSet("test_set")],
        )

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
            plugins=[
                AdkAgentPlugin(
                    activity_options={"start_to_close_timeout": timedelta(minutes=2)}
                )
            ],
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

        return last_event


class McpModel(BaseLlm):
    responses: list[LlmResponse] = [
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
    response_iter: Iterator[LlmResponse] = iter(responses)

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["weather_model"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield next(self.response_iter)


@pytest.mark.parametrize("use_local_model", [True, False])
@pytest.mark.asyncio
async def test_mcp_agent(client: Client, use_local_model: bool):
    if not use_local_model and not os.environ.get("GOOGLE_API_KEY"):
        pytest.skip("No google API key")

    new_config = client.config()
    new_config["plugins"] = [
        TemporalAdkPlugin(
            toolset_providers=[
                TemporalMcpToolSetProvider(
                    "test_set",
                    lambda _: McpToolset(
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
                        require_confirmation=True,
                    ),
                )
            ]
        )
    ]
    client = Client(**new_config)

    # Run Worker with the ADK plugin
    async with Worker(
        client,
        task_queue="adk-task-queue",
        workflows=[McpAgent],
        max_cached_workflows=0,
    ):
        if use_local_model:
            LLMRegistry.register(ResearchModel)

        # Test Multi Agent
        handle = await client.start_workflow(
            McpAgent.run,
            args=[
                "What files are in the current directory?",
                "research_model" if use_local_model else "gemini-2.5-pro",
            ],
            id=f"mcp-agent-workflow-{uuid.uuid4()}",
            task_queue="adk-task-queue",
        )
        result = await handle.result()
        print(f"MCP-Agent Workflow result: {result}")
        if use_local_model:
            assert result == "haiku"


@pytest.mark.asyncio
async def test_single_agent_telemetry(client: Client):
    exporter = InMemorySpanExporter()
    new_config = client.config()
    new_config["plugins"] = [TemporalAdkPlugin(otel_exporters=[exporter])]
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
        LLMRegistry.register(WeatherModel)

        # Test Weather Agent
        handle = await client.start_workflow(
            WeatherAgent.run,
            args=[
                "What is the weather in New York?",
                "weather_model",
            ],
            id=f"weather-agent-workflow-{uuid.uuid4()}",
            task_queue="adk-task-queue",
        )
        result = await handle.result()
        print(f"Workflow result: {result}")

        assert result is not None
        assert result.content is not None
        assert result.content.parts is not None
        assert result.content.parts[0].text == "warm and sunny"

    spans = exporter.get_finished_spans()

    invocation_span = next(
        (s for s in spans if s.name == "invocation [test_app]"), None
    )
    agent_span = next((s for s in spans if s.name == "agent_run [test_agent]"), None)
    _llm_spans = [s for s in spans if s.name == "call_llm"]
    tool_spans = [s for s in spans if "execute_tool" in s.name]

    assert invocation_span is not None
    assert invocation_span.parent is None
    assert invocation_span.context is not None

    assert agent_span is not None
    assert agent_span.parent is not None
    assert agent_span.context is not None
    assert agent_span.parent.span_id == invocation_span.context.span_id

    # Model is invoked twice, but because of before_model_callback, llm spans are not reported
    # assert len(llm_spans) == 2

    assert len(tool_spans) == 1
    assert tool_spans[0].parent is not None
    assert tool_spans[0].parent.span_id == agent_span.context.span_id
