"""Integration tests for ADK streaming support.

Verifies that the streaming model activity publishes TEXT_DELTA events via
PubSubMixin and that non-streaming mode remains backward-compatible.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta

import pytest
from google.adk import Agent
from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai.types import Content, Part

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.google_adk_agents import GoogleAdkPlugin, TemporalModel
from temporalio.contrib.pubsub import PubSubClient, PubSubMixin
from temporalio.worker import Worker

logger = logging.getLogger(__name__)


class StreamingTestModel(BaseLlm):
    """Test model that yields multiple partial responses to simulate streaming."""

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["streaming_test_model"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        yield LlmResponse(
            content=Content(role="model", parts=[Part(text="Hello ")])
        )
        yield LlmResponse(
            content=Content(role="model", parts=[Part(text="world!")])
        )


@workflow.defn
class StreamingAdkWorkflow(PubSubMixin):
    """Test workflow that uses streaming TemporalModel with PubSubMixin."""

    @workflow.init
    def __init__(self, prompt: str) -> None:
        self.init_pubsub()

    @workflow.run
    async def run(self, prompt: str) -> str:
        model = TemporalModel("streaming_test_model", streaming=True)
        agent = Agent(
            name="test_agent",
            model=model,
            instruction="You are a test agent.",
        )

        runner = InMemoryRunner(agent=agent, app_name="test-app")
        session = await runner.session_service.create_session(
            app_name="test-app", user_id="test"
        )

        final_text = ""
        async for event in runner.run_async(
            user_id="test",
            session_id=session.id,
            new_message=Content(role="user", parts=[Part(text=prompt)]),
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        final_text = part.text

        return final_text


@workflow.defn
class NonStreamingAdkWorkflow:
    """Test workflow without streaming -- verifies backward compatibility."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        model = TemporalModel("streaming_test_model", streaming=False)
        agent = Agent(
            name="test_agent",
            model=model,
            instruction="You are a test agent.",
        )

        runner = InMemoryRunner(agent=agent, app_name="test-app")
        session = await runner.session_service.create_session(
            app_name="test-app", user_id="test"
        )

        final_text = ""
        async for event in runner.run_async(
            user_id="test",
            session_id=session.id,
            new_message=Content(role="user", parts=[Part(text=prompt)]),
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        final_text = part.text

        return final_text


@pytest.mark.asyncio
async def test_streaming_publishes_events(client: Client):
    """Verify that streaming activity publishes TEXT_DELTA events via pubsub."""
    LLMRegistry.register(StreamingTestModel)

    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    workflow_id = f"adk-streaming-test-{uuid.uuid4()}"

    async with Worker(
        client,
        task_queue="adk-streaming-test",
        workflows=[StreamingAdkWorkflow],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            StreamingAdkWorkflow.run,
            "Hello",
            id=workflow_id,
            task_queue="adk-streaming-test",
            execution_timeout=timedelta(seconds=30),
        )

        # Subscribe concurrently while the workflow is running
        pubsub = PubSubClient.create(client, workflow_id)
        events: list[dict] = []

        async def collect_events() -> None:
            async for item in pubsub.subscribe(
                ["events"], from_offset=0, poll_cooldown=0.05
            ):
                event = json.loads(item.data)
                events.append(event)
                if event["type"] == "LLM_CALL_COMPLETE":
                    break

        collect_task = asyncio.create_task(collect_events())
        result = await handle.result()

        # Wait for event collection with a timeout
        await asyncio.wait_for(collect_task, timeout=10.0)

    assert result is not None

    event_types = [e["type"] for e in events]
    assert "LLM_CALL_START" in event_types, f"Expected LLM_CALL_START, got: {event_types}"
    assert "TEXT_DELTA" in event_types, f"Expected TEXT_DELTA, got: {event_types}"
    assert "LLM_CALL_COMPLETE" in event_types, (
        f"Expected LLM_CALL_COMPLETE, got: {event_types}"
    )

    text_deltas = [e["data"]["delta"] for e in events if e["type"] == "TEXT_DELTA"]
    assert len(text_deltas) >= 1, f"Expected at least 1 TEXT_DELTA, got: {text_deltas}"


@pytest.mark.asyncio
async def test_non_streaming_backward_compatible(client: Client):
    """Verify non-streaming mode still works (backward compatibility)."""
    LLMRegistry.register(StreamingTestModel)

    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    async with Worker(
        client,
        task_queue="adk-non-streaming-test",
        workflows=[NonStreamingAdkWorkflow],
        max_cached_workflows=0,
    ):
        handle = await client.start_workflow(
            NonStreamingAdkWorkflow.run,
            "Hello",
            id=f"adk-non-streaming-test-{uuid.uuid4()}",
            task_queue="adk-non-streaming-test",
            execution_timeout=timedelta(seconds=30),
        )
        result = await handle.result()

    assert result is not None
