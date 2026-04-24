"""Integration tests for OpenAI Agents streaming support.

The streaming activity publishes raw OpenAI stream events to the pubsub
side channel; consumers parse them directly. These tests verify that the
events arrive intact and that the workflow still returns the right final
result from the ResponseCompletedEvent.
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
    ResponseTextConfig,
    ResponseTextDeltaEvent,
    ResponseUsage,
)
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
)
from openai.types.shared.response_format_text import ResponseFormatText

from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.openai_agents import ModelActivityParameters
from temporalio.contrib.openai_agents.testing import AgentEnvironment
from temporalio.contrib.pubsub import PubSub, PubSubClient
from temporalio.exceptions import ApplicationError
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
            text=ResponseTextConfig(format=ResponseFormatText(type="text")),
            truncation="disabled",
            usage=ResponseUsage(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                input_tokens_details=InputTokensDetails(cached_tokens=0),
                output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
            ),
        )
        yield ResponseCompletedEvent(
            response=response, sequence_number=2, type="response.completed"
        )


@workflow.defn
class StreamingOpenAIWorkflow:
    """Test workflow that uses streaming model activity with PubSub."""

    @workflow.init
    def __init__(self, prompt: str) -> None:
        self.pubsub = PubSub()

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
async def test_streaming_publishes_raw_events(client: Client):
    """Every event from model.stream_response() lands on the pubsub topic
    as its native OpenAI Pydantic JSON, and the workflow gets the final
    text from the ResponseCompletedEvent."""
    async with AgentEnvironment(
        model=StreamingTestModel(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            enable_streaming=True,
        ),
    ) as env:
        client = env.applied_on_client(client)
        workflow_id = f"openai-streaming-test-{uuid.uuid4()}"

        async with new_worker(
            client, StreamingOpenAIWorkflow, max_cached_workflows=0
        ) as worker:
            handle = await client.start_workflow(
                StreamingOpenAIWorkflow.run,
                "Hello",
                id=workflow_id,
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )

            pubsub = PubSubClient.create(client, workflow_id)
            events: list[dict] = []

            async def collect_events() -> None:
                async for item in pubsub.subscribe(
                    ["events"], from_offset=0, result_type=bytes, poll_cooldown=0.05
                ):
                    event = json.loads(item.data)
                    events.append(event)
                    if event["type"] == "response.completed":
                        break

            collect_task = asyncio.create_task(collect_events())
            result = await handle.result()
            await asyncio.wait_for(collect_task, timeout=10.0)

    assert result == "Hello world!"

    # Exact event sequence matches what StreamingTestModel yields — no
    # normalization, no synthesized brackets.
    types_in_order = [e["type"] for e in events]
    assert types_in_order == [
        "response.output_text.delta",
        "response.output_text.delta",
        "response.completed",
    ], f"Unexpected event sequence: {types_in_order}"

    deltas = [e["delta"] for e in events if e["type"] == "response.output_text.delta"]
    assert deltas == ["Hello ", "world!"]


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


class TruncatedStreamingTestModel(Model):
    """Fake model whose stream ends without a ResponseCompletedEvent."""

    __test__ = False

    async def get_response(self, *a: Any, **kw: Any) -> ModelResponse:
        raise NotImplementedError

    async def stream_response(
        self, *a: Any, **kw: Any
    ) -> AsyncIterator[TResponseStreamEvent]:
        yield ResponseTextDeltaEvent(
            content_index=0,
            delta="partial",
            item_id="item1",
            output_index=0,
            sequence_number=0,
            type="response.output_text.delta",
            logprobs=[],
        )


@pytest.mark.asyncio
async def test_streaming_raises_when_no_completed_event(client: Client):
    """A stream that ends without ResponseCompletedEvent surfaces as a
    non-retryable ApplicationError on the workflow."""
    async with AgentEnvironment(
        model=TruncatedStreamingTestModel(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            enable_streaming=True,
        ),
    ) as env:
        client = env.applied_on_client(client)
        async with new_worker(
            client, StreamingOpenAIWorkflow, max_cached_workflows=0
        ) as worker:
            with pytest.raises(WorkflowFailureError) as exc_info:
                await client.execute_workflow(
                    StreamingOpenAIWorkflow.run,
                    "Hi",
                    id=f"openai-streaming-truncated-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=30),
                )

    # Unwrap: WorkflowFailureError -> ActivityError -> ApplicationError
    cause = exc_info.value.__cause__
    while cause is not None and not isinstance(cause, ApplicationError):
        cause = cause.__cause__
    assert isinstance(
        cause, ApplicationError
    ), f"Expected ApplicationError cause, got {exc_info.value!r}"
    assert "Stream ended without ResponseCompletedEvent" in str(cause)
    assert cause.non_retryable is True
