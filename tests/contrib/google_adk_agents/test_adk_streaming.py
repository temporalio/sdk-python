"""Integration tests for ADK streaming support.

Verifies that the streaming model activity publishes raw ``LlmResponse``
chunks via the WorkflowStream broker. Non-streaming behavior is covered
by ``test_google_adk_agents.py``.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta

import pytest
from google.adk import Agent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai.types import Content, Part

from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.google_adk_agents import GoogleAdkPlugin, TemporalModel
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
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
        # The streaming activity must call us with stream=True; if a
        # regression drops the flag this test should fail.
        if not stream:
            raise AssertionError(
                "StreamingTestModel.generate_content_async requires stream=True"
            )
        yield LlmResponse(content=Content(role="model", parts=[Part(text="Hello ")]))
        yield LlmResponse(content=Content(role="model", parts=[Part(text="world!")]))


@workflow.defn
class StreamingAdkWorkflow:
    """Test workflow that opts into streaming via RunConfig.streaming_mode."""

    @workflow.init
    def __init__(self, prompt: str) -> None:
        self.stream = WorkflowStream()

    @workflow.run
    async def run(self, prompt: str) -> str:
        model = TemporalModel("streaming_test_model", streaming_event_topic="events")
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
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        final_text = part.text

        return final_text


@pytest.mark.asyncio
async def test_streaming_publishes_events(client: Client):
    """Streaming activity publishes raw LlmResponse chunks to the topic."""
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

        stream = WorkflowStreamClient.create(client, workflow_id)
        responses: list[LlmResponse] = []

        async def collect_events() -> None:
            async for item in stream.subscribe(
                ["events"],
                from_offset=0,
                result_type=LlmResponse,
                poll_cooldown=timedelta(milliseconds=50),
            ):
                responses.append(item.data)
                if len(responses) >= 2:
                    break

        collect_task = asyncio.create_task(collect_events())
        result = await handle.result()
        await asyncio.wait_for(collect_task, timeout=10.0)

    # Workflow assembles streamed parts; the last part it observes is "world!".
    assert result == "world!"

    texts: list[str] = []
    for r in responses:
        if r.content and r.content.parts:
            for part in r.content.parts:
                if part.text:
                    texts.append(part.text)
    assert texts == ["Hello ", "world!"], f"Unexpected text deltas: {texts}"


@workflow.defn
class StreamingAdkRequiresTopicWorkflow:
    """Calls ``generate_content_async(stream=True)`` without configuring
    ``streaming_event_topic``; the call must raise before any activity
    is scheduled."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        model = TemporalModel("streaming_test_model")
        agent = Agent(
            name="test_agent",
            model=model,
            instruction="You are a test agent.",
        )
        runner = InMemoryRunner(agent=agent, app_name="test-app")
        session = await runner.session_service.create_session(
            app_name="test-app", user_id="test"
        )
        async for _ in runner.run_async(
            user_id="test",
            session_id=session.id,
            new_message=Content(role="user", parts=[Part(text=prompt)]),
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        ):
            pass
        return "should not reach"


@pytest.mark.asyncio
async def test_streaming_requires_topic(client: Client):
    """``stream=True`` fails fast when no streaming topic was configured
    on ``TemporalModel``. The error is raised in the workflow before any
    streaming activity is scheduled."""
    LLMRegistry.register(StreamingTestModel)

    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    async with Worker(
        client,
        task_queue="adk-streaming-requires-topic",
        workflows=[StreamingAdkRequiresTopicWorkflow],
        max_cached_workflows=0,
    ):
        with pytest.raises(WorkflowFailureError) as exc_info:
            await client.execute_workflow(
                StreamingAdkRequiresTopicWorkflow.run,
                "Hi",
                id=f"adk-streaming-requires-topic-{uuid.uuid4()}",
                task_queue="adk-streaming-requires-topic",
                execution_timeout=timedelta(seconds=30),
            )

    assert "streaming_event_topic" in str(exc_info.value.cause)
