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

import dataclasses
import logging
import uuid
from datetime import timedelta

import pytest
from google.adk import Agent, Runner
from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService
from google.adk.utils.context_utils import Aclosing
from google.genai import types

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.google_adk_agents import AgentPlugin, GoogleAdkPlugin
from temporalio.contrib.pydantic import (
    PydanticPayloadConverter,
)
from temporalio.converter import DataConverter
from temporalio.worker import Worker

# Required Environment Variables for this test:
# in this folder update .env.example to be .env and have the following vars:
# GOOGLE_GENAI_USE_VERTEXAI=1
# GOOGLE_CLOUD_PROJECT="<your-project>"
# GOOGLE_CLOUD_LOCATION="<your-location>"
# TEST_BACKEND=VERTEX_ONLY
# then:
# start temporal: temporal server start-dev
# then:
# uv run pytest tests/integration/manual_test_temporal_integration.py


logger = logging.getLogger(__name__)


@activity.defn
async def get_weather(city: str) -> str:
    """Activity that gets weather for a given city."""
    return "Warm and sunny. 17 degrees."


@workflow.defn
class WeatherAgent:
    @workflow.run
    async def run(self, prompt: str) -> Event | None:
        logger.info("Workflow started.")

        # 1. Define Agent using Temporal Helpers
        # Note: AgentPlugin in the Runner automatically handles Runtime setup
        # and Model Activity interception. We use standard ADK models now.

        # Wraps 'get_weather' activity as a Tool
        weather_tool = AgentPlugin.activity_tool(
            get_weather, start_to_close_timeout=timedelta(seconds=60)
        )

        agent = Agent(
            name="test_agent",
            model="gemini-2.5-pro",  # Standard model string
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
                AgentPlugin(
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
    async def run(self, topic: str) -> str:
        # Example of multi-turn/multi-agent orchestration
        # This is where Temporal shines - orchestrating complex agent flows

        # 0. Deterministic Runtime is now auto-configured by AdkInterceptor!

        # 1. Setup Session Service
        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="multi_agent_app", user_id="test_user"
        )

        # 2. Define Agents
        # Sub-agent: Researcher
        researcher = LlmAgent(
            name="researcher",
            model="gemini-2.5-pro",
            instruction="You are a researcher. Find information about the topic.",
        )

        # Sub-agent: Writer
        writer = LlmAgent(
            name="writer",
            model="gemini-2.5-pro",
            instruction="You are a poet. Write a haiku based on the research.",
        )

        # Root Agent: Coordinator
        coordinator = LlmAgent(
            name="coordinator",
            model="gemini-2.5-pro",
            instruction="You are a coordinator. Delegate to researcher then writer.",
            sub_agents=[researcher, writer],
        )

        # 3. Initialize Runner with required args
        runner = Runner(
            agent=coordinator,
            app_name="multi_agent_app",
            session_service=session_service,
            plugins=[
                AgentPlugin(
                    activity_options={"start_to_close_timeout": timedelta(minutes=2)}
                )
            ],
        )

        # 4. Run
        # Note: In a real temporal app, we might signal the workflow or use queries.
        # Here we just run a single turn for the test.
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
            if event.content and event.content.parts:
                final_content = event.content.parts[0].text

        return final_content


@pytest.mark.asyncio
async def test_temporal_integration():
    """Manual integration test requiring a running Temporal server."""

    # 1. Start a Worker (in a real app, this would be a separate process)
    # We run it here for the test.

    try:
        # Connect to Temporal Server
        # We must configure the data converter to handle Pydantic models (like Event)
        client = await Client.connect(
            "localhost:7233",
            data_converter=DataConverter(
                payload_converter_class=PydanticPayloadConverter
            ),
        )
    except RuntimeError:
        pytest.skip("Could not connect to Temporal server. Is it running?")

    # Run Worker with the ADK plugin
    async with Worker(
        client,
        task_queue="adk-task-queue",
        activities=[
            get_weather,
        ],
        workflows=[WeatherAgent, MultiAgentWorkflow],
        plugins=[GoogleAdkPlugin()],
    ):
        print("Worker started.")
        # Test Weather Agent
        result = await client.execute_workflow(
            WeatherAgent.run,
            "What is the weather in New York?",
            id=f"weather-agent-workflow-{uuid.uuid4()}",
            task_queue="adk-task-queue",
        )
        print(f"Workflow result: {result}")

        # Test Multi Agent
        result_multi = await client.execute_workflow(
            MultiAgentWorkflow.run,
            "Run mult-agent flow",
            id=f"multi-agent-workflow-{uuid.uuid4()}",
            task_queue="adk-task-queue",
        )
        print(f"Multi-Agent Workflow result: {result_multi}")
