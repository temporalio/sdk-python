"""Streaming tests for the Google Gemini SDK Temporal integration.

Covers ``generate_content_stream`` publishing each chunk to a
:class:`~temporalio.contrib.workflow_streams.WorkflowStream` topic for external
consumers, and the fail-fast when ``streaming_topic`` is set without a hosted
``WorkflowStream``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from google.genai import types

from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.google_genai import TemporalAsyncClient
from temporalio.contrib.google_genai.testing import GeminiTestServer, text_response
from temporalio.contrib.workflow_streams import WorkflowStream, WorkflowStreamClient
from tests.helpers import new_worker


@workflow.defn
class StreamingWorkflowStreamWorkflow:
    """Streams generate_content_stream chunks to a WorkflowStream topic.

    Holds the run open on a ``finish`` signal so an external subscriber can
    reliably consume the published chunk before the workflow completes.
    """

    @workflow.init
    def __init__(self, prompt: str) -> None:
        self.stream = WorkflowStream()
        self._done = False

    @workflow.run
    async def run(self, prompt: str) -> str:
        client = TemporalAsyncClient(streaming_topic="gemini")
        out: list[str] = []
        async for chunk in await client.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=prompt,
        ):
            out.append(chunk.text or "")
        await workflow.wait_condition(lambda: self._done)
        return "".join(out)

    @workflow.signal
    def finish(self) -> None:
        self._done = True


@workflow.defn
class StreamingNoStreamWorkflow:
    """Sets streaming_topic but hosts no WorkflowStream — must fail fast."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        client = TemporalAsyncClient(streaming_topic="gemini")
        async for _ in await client.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=prompt,
        ):
            pass
        return "done"


async def test_streaming_publishes_to_workflow_stream(client: Client):
    """Streamed chunks are published to the WorkflowStream for external consumers."""
    server = GeminiTestServer([text_response("Hello from Gemini stream")])
    config = client.config()
    config["plugins"] = [server.plugin()]
    new_client = Client(**config)

    async with new_worker(new_client, StreamingWorkflowStreamWorkflow) as worker:
        wf_id = f"gemini-stream-{uuid.uuid4()}"
        handle = await new_client.start_workflow(
            StreamingWorkflowStreamWorkflow.run,
            "say hi",
            id=wf_id,
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=15),
        )

        stream = WorkflowStreamClient.create(new_client, wf_id)
        received: list[types.GenerateContentResponse] = []
        async for item in stream.subscribe(
            ["gemini"],
            result_type=types.GenerateContentResponse,
            poll_cooldown=timedelta(milliseconds=20),
        ):
            received.append(item.data)
            break  # one scripted chunk

        await handle.signal(StreamingWorkflowStreamWorkflow.finish)
        result = await handle.result()

    assert result == "Hello from Gemini stream"
    assert len(received) == 1
    assert received[0].text == "Hello from Gemini stream"


async def test_streaming_without_workflow_stream_raises(client: Client):
    """streaming_topic set but no WorkflowStream hosted fails the workflow."""
    server = GeminiTestServer([text_response("unused")])
    config = client.config()
    config["plugins"] = [server.plugin()]
    new_client = Client(**config)

    async with new_worker(new_client, StreamingNoStreamWorkflow) as worker:
        with pytest.raises(WorkflowFailureError) as exc_info:
            await new_client.execute_workflow(
                StreamingNoStreamWorkflow.run,
                "hi",
                id=f"gemini-stream-nostream-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )

    assert "WorkflowStream" in str(exc_info.value.cause)
