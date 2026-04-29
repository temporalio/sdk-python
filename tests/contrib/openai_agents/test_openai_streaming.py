"""Integration tests for OpenAI Agents streaming support.

Streaming is opt-in via ``Runner.run_streamed``. Events flow back to the
workflow through ``RunResultStreaming.stream_events()`` (in batch after
each model activity completes) and to external consumers in real time
via the configured stream topic.
"""

import asyncio
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
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
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
    """Test workflow that opts into streaming via ``Runner.run_streamed``.

    Workflow code consumes events from ``stream_events()`` and exposes
    the seen event types via a query so the test can verify both the
    in-workflow iteration and the external stream subscriber observe the
    same events.
    """

    @workflow.init
    def __init__(self, prompt: str) -> None:
        self.stream = WorkflowStream()
        self.workflow_event_types: list[str] = []

    @workflow.run
    async def run(self, prompt: str) -> str:
        agent = Agent[None](
            name="Assistant",
            instructions="You are a test agent.",
        )
        result = Runner.run_streamed(starting_agent=agent, input=prompt)
        async for event in result.stream_events():
            raw = getattr(event, "data", None)
            event_type = getattr(raw, "type", None)
            if event_type is not None:
                self.workflow_event_types.append(event_type)
        return result.final_output

    @workflow.query
    def get_workflow_event_types(self) -> list[str]:
        return self.workflow_event_types


@workflow.defn
class StreamingRequiresTopicWorkflow:
    """Workflow that opts into ``Runner.run_streamed`` while the model
    plugin was configured without a ``streaming_event_topic``.

    The stub raises before scheduling the streaming activity; this
    propagates out of ``Runner.run_streamed`` and fails the workflow.
    """

    @workflow.run
    async def run(self, prompt: str) -> str:
        agent = Agent[None](
            name="Assistant",
            instructions="You are a test agent.",
        )
        result = Runner.run_streamed(starting_agent=agent, input=prompt)
        async for _ in result.stream_events():
            pass
        return result.final_output


@pytest.mark.asyncio
async def test_streaming_publishes_raw_events(client: Client):
    """Both the workflow consumer (via stream_events) and the stream
    topic see the same native OpenAI events, in order, with no
    normalization."""
    async with AgentEnvironment(
        model=StreamingTestModel(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            streaming_event_topic="events",
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

            stream = WorkflowStreamClient.create(client, workflow_id)
            published: list[TResponseStreamEvent] = []

            async def collect_events() -> None:
                async for item in stream.subscribe(
                    ["events"],
                    from_offset=0,
                    # TResponseStreamEvent is a discriminated union
                    # (Annotated[..., Discriminator]); Pydantic decodes
                    # it via TypeAdapter at runtime, but pyright sees
                    # ``Annotated`` rather than ``type``.
                    result_type=TResponseStreamEvent,  # type: ignore[arg-type]
                    poll_cooldown=timedelta(milliseconds=50),
                ):
                    published.append(item.data)
                    if item.data.type == "response.completed":
                        break

            collect_task = asyncio.create_task(collect_events())
            result = await handle.result()
            await asyncio.wait_for(collect_task, timeout=10.0)

            workflow_event_types = await handle.query(
                StreamingOpenAIWorkflow.get_workflow_event_types
            )

    assert result == "Hello world!"

    published_types = [e.type for e in published]
    assert published_types == [
        "response.output_text.delta",
        "response.output_text.delta",
        "response.completed",
    ], f"Unexpected published event sequence: {published_types}"

    deltas = [e.delta for e in published if e.type == "response.output_text.delta"]
    assert deltas == ["Hello ", "world!"]

    # Workflow-side iteration sees the same model events in the same order.
    assert workflow_event_types == published_types


@pytest.mark.asyncio
async def test_streaming_requires_topic(client: Client):
    """``Runner.run_streamed`` fails fast when the plugin has no topic
    configured. The error is raised in ``stream_response`` before any
    streaming activity is scheduled."""
    async with AgentEnvironment(
        model=StreamingTestModel(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            streaming_event_topic=None,
        ),
    ) as env:
        client = env.applied_on_client(client)
        async with new_worker(
            client, StreamingRequiresTopicWorkflow, max_cached_workflows=0
        ) as worker:
            with pytest.raises(WorkflowFailureError) as exc_info:
                await client.execute_workflow(
                    StreamingRequiresTopicWorkflow.run,
                    "Hi",
                    id=f"openai-streaming-requires-topic-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=30),
                )

    assert "streaming_event_topic" in str(exc_info.value.cause)


@pytest.mark.asyncio
async def test_streaming_rejects_local_activity(client: Client):
    """``Runner.run_streamed`` fails fast when the plugin is configured
    with ``use_local_activity=True``. Local activities support neither
    heartbeats nor the workflow-stream signal channel."""
    async with AgentEnvironment(
        model=StreamingTestModel(),
        model_params=ModelActivityParameters(
            start_to_close_timeout=timedelta(seconds=30),
            streaming_event_topic="events",
            use_local_activity=True,
        ),
    ) as env:
        client = env.applied_on_client(client)
        async with new_worker(
            client, StreamingRequiresTopicWorkflow, max_cached_workflows=0
        ) as worker:
            with pytest.raises(WorkflowFailureError) as exc_info:
                await client.execute_workflow(
                    StreamingRequiresTopicWorkflow.run,
                    "Hi",
                    id=f"openai-streaming-rejects-local-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                    execution_timeout=timedelta(seconds=30),
                )

    assert "use_local_activity" in str(exc_info.value.cause)
