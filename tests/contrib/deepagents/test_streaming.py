"""Streaming: model calls route through the streaming activity when a topic is set.

Setting ``streaming_topic`` flips model dispatch from ``invoke_model`` to
``invoke_model_streaming``, which streams chunks out of the activity and returns
the aggregated final message to the workflow (so the durable result matches the
non-streaming path). These assert the observable effects: which activity is
scheduled, the aggregated content, and that a custom batch interval is threaded
into the streaming activity.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)

pytest.importorskip("langchain_core")

from temporalio import workflow  # noqa: E402
from temporalio.worker import Worker  # noqa: E402
from tests.contrib.deepagents.helpers import count_scheduled_activities  # noqa: E402

with workflow.unsafe.imports_passed_through():
    from langchain_core.messages import HumanMessage

    from temporalio.contrib.deepagents import DeepAgentsPlugin, TemporalModel
    from temporalio.contrib.deepagents.testing import mock_model_provider

INVOKE_MODEL = "deepagents.invoke_model"
INVOKE_MODEL_STREAMING = "deepagents.invoke_model_streaming"


@workflow.defn
class StreamWorkflow:
    @workflow.run
    async def run(self, prompt: str) -> str:
        model = TemporalModel(model="fake:model")
        parts: list[str] = []
        async for chunk in model.astream([HumanMessage(content=prompt)]):
            parts.append(str(chunk.content))
        return "".join(parts)


@pytest.mark.asyncio
async def test_stream_chunks_published(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider(["Streamed answer."]),
        streaming_topic="da-stream-topic",
        model_activity_options={"start_to_close_timeout": timedelta(seconds=30)},
    )
    async with Worker(
        env.client,
        task_queue="da-stream",
        workflows=[StreamWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            StreamWorkflow.run,
            "stream please",
            id=f"da-stream-{uuid.uuid4()}",
            task_queue="da-stream",
        )
        out = await handle.result()

    assert "Streamed answer." in out
    counts = await count_scheduled_activities(handle)
    # The topic is set, so dispatch used the streaming activity, not invoke_model.
    assert counts[INVOKE_MODEL_STREAMING] == 1, counts
    assert counts[INVOKE_MODEL] == 0, counts


@pytest.mark.asyncio
async def test_batch_interval_coalesces(env: WorkflowEnvironment) -> None:
    # A custom batch interval is threaded into the streaming activity that
    # coalesces chunks; streaming still returns the aggregated message.
    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider(["Batched."]),
        streaming_topic="da-batch-topic",
        streaming_batch_interval=timedelta(milliseconds=500),
    )
    assert plugin._activities._streaming_batch_interval == timedelta(milliseconds=500)

    async with Worker(
        env.client,
        task_queue="da-batch",
        workflows=[StreamWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            StreamWorkflow.run,
            "batch please",
            id=f"da-batch-{uuid.uuid4()}",
            task_queue="da-batch",
        )
        out = await handle.result()

    assert "Batched." in out
    counts = await count_scheduled_activities(handle)
    assert counts[INVOKE_MODEL_STREAMING] == 1, counts
