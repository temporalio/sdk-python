"""Integration tests for OpenAI Agents streaming support.

Verifies that the streaming model activity publishes TEXT_DELTA events via
PubSubMixin and that the workflow returns the correct final result.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from agents import (
    Agent,
    AgentOutputSchemaBase,
    Handoff,
    Model,
    ModelResponse,
    ModelSettings,
    ModelTracing,
    Runner,
    Tool,
    TResponseInputItem,
    Usage,
)
from agents.items import TResponseStreamEvent
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
)

from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.openai_agents import ModelActivityParameters
from temporalio.contrib.openai_agents.testing import AgentEnvironment
from temporalio.contrib.pubsub import PubSubClient, PubSubMixin
from tests.helpers import new_worker

logger = logging.getLogger(__name__)


class StreamingTestModel(Model):
    """Test model that yields text deltas followed by a ResponseCompletedEvent."""

    __test__ = False

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs: Any,
    ) -> ModelResponse:
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="msg_test",
                    content=[
                        ResponseOutputText(
                            text="Hello world!",
                            annotations=[],
                            type="output_text",
                            logprobs=[],
                        )
                    ],
                    role="assistant",
                    status="completed",
                    type="message",
                )
            ],
            usage=Usage(),
            response_id=None,
        )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        **kwargs: Any,
    ) -> AsyncIterator[TResponseStreamEvent]:
        # Yield text deltas
        yield ResponseTextDeltaEvent(
            content_index=0,
            delta="Hello ",
            item_id="item1",
            output_index=0,
            sequence_number=0,
            type="response.output_text.delta",
            logprobs=[],
        )
        yield ResponseTextDeltaEvent(
            content_index=0,
            delta="world!",
            item_id="item1",
            output_index=0,
            sequence_number=1,
            type="response.output_text.delta",
            logprobs=[],
        )

        # Yield the final completed event
        response = Response(
            id="resp_test",
            created_at=0,
            error=None,
            incomplete_details=None,
            instructions=None,
            metadata={},
            model="test",
            object="response",
            output=[
                ResponseOutputMessage(
                    id="msg_test",
                    content=[
                        ResponseOutputText(
                            text="Hello world!",
                            annotations=[],
                            type="output_text",
                            logprobs=[],
                        )
                    ],
                    role="assistant",
                    status="completed",
                    type="message",
                )
            ],
            parallel_tool_calls=True,
            temperature=1.0,
            tool_choice="auto",
            tools=[],
            top_p=1.0,
            status="completed",
            text={"format": {"type": "text"}},
            truncation="disabled",
            usage={
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        )
        yield ResponseCompletedEvent(
            response=response, sequence_number=2, type="response.completed"
        )


@workflow.defn
class StreamingOpenAIWorkflow(PubSubMixin):
    """Test workflow that uses streaming model activity with PubSubMixin."""

    @workflow.init
    def __init__(self, prompt: str) -> None:
        self.init_pubsub()

    @workflow.run
    async def run(self, prompt: str) -> str:
        agent = Agent[None](
            name="Assistant",
            instructions="You are a test agent.",
        )
        result = await Runner.run(starting_agent=agent, input=prompt)
        return result.final_output


@workflow.defn
class NonStreamingOpenAIWorkflow:
    """Test workflow without streaming -- verifies backward compatibility."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        agent = Agent[None](
            name="Assistant",
            instructions="You are a test agent.",
        )
        result = await Runner.run(starting_agent=agent, input=prompt)
        return result.final_output


@pytest.mark.asyncio
async def test_streaming_publishes_events(client: Client):
    """Verify that streaming activity publishes TEXT_DELTA events via pubsub."""
    model = StreamingTestModel()
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            enable_streaming=True,
        ),
    ) as env:
        client = env.applied_on_client(client)

        workflow_id = f"openai-streaming-test-{uuid.uuid4()}"

        async with new_worker(
            client,
            StreamingOpenAIWorkflow,
            max_cached_workflows=0,
        ) as worker:
            handle = await client.start_workflow(
                StreamingOpenAIWorkflow.run,
                "Hello",
                id=workflow_id,
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            # Subscribe concurrently while the workflow is running
            pubsub = PubSubClient.create(client, workflow_id)
            events: list[dict] = []

            async def collect_events() -> None:
                async for item in pubsub.subscribe(
                    ["events"], from_offset=0, result_type=bytes, poll_cooldown=0.05
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
    assert "LLM_CALL_START" in event_types, (
        f"Expected LLM_CALL_START, got: {event_types}"
    )
    assert "TEXT_DELTA" in event_types, (
        f"Expected TEXT_DELTA, got: {event_types}"
    )
    assert "LLM_CALL_COMPLETE" in event_types, (
        f"Expected LLM_CALL_COMPLETE, got: {event_types}"
    )

    text_deltas = [e["data"]["delta"] for e in events if e["type"] == "TEXT_DELTA"]
    assert len(text_deltas) >= 1, f"Expected at least 1 TEXT_DELTA, got: {text_deltas}"
    assert "Hello " in text_deltas
    assert "world!" in text_deltas


@pytest.mark.asyncio
async def test_non_streaming_backward_compatible(client: Client):
    """Verify non-streaming mode still works (backward compatibility)."""
    model = StreamingTestModel()
    async with AgentEnvironment(
        model=model,
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            enable_streaming=False,
        ),
    ) as env:
        client = env.applied_on_client(client)

        async with new_worker(
            client,
            NonStreamingOpenAIWorkflow,
            max_cached_workflows=0,
        ) as worker:
            result = await client.execute_workflow(
                NonStreamingOpenAIWorkflow.run,
                "Hello",
                id=f"openai-non-streaming-test-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

    assert result == "Hello world!"
