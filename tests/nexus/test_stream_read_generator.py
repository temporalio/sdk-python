"""A generator in Workflow code that reads the Stream of an Activity that still runs.

A library that reads a model through an async generator, such as ADK, needs this shape. The
generator loops ``take``, so Workflow code gets the chunks in batches while the Activity runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.nexus.system.stream_service import StreamRef
from temporalio.testing import MemoryStreamStore
from tests.nexus.test_stream_read_workflow import _markers, _with_store

with workflow.unsafe.imports_passed_through():
    from tests.helpers import new_worker


@dataclass
class ModelCall:
    chunks_id: str
    seen_id: str


@activity.defn
async def stream_model_output(call: ModelCall) -> None:
    client = activity.client()
    chunks = client.get_stream(StreamRef(id=call.chunks_id), body_type=str)
    seen = client.get_stream(StreamRef(id=call.seen_id), body_type=str)
    await chunks.append("Hel", "lo")
    # The Activity writes the rest only after the Workflow says that it has the first chunks.
    # So the test passes only if Workflow code gets chunks while the Activity still runs.
    told = False
    async for _ in seen.iter_read_items(wait=timedelta(seconds=30)):
        told = True
        break
    if not told:
        raise ApplicationError(
            "The Workflow got no chunk while the Activity ran", non_retryable=True
        )
    await chunks.append(", ", "world")
    await chunks.close()


async def model_chunks(chunks_id: str, batches: list[int]) -> AsyncGenerator[str, None]:
    """Yield each chunk of the model's Stream, as the model stub of such a library does."""
    stream = workflow.get_stream(StreamRef(id=chunks_id), body_type=str)
    last = None
    while True:
        read = await stream.take(timedelta(seconds=1), from_offset=last)
        last = read.last_offset
        if read.records:
            batches.append(len(read.records))
        for record in read.records:
            yield record.body
        if read.done:
            return


@workflow.defn
class LiveModelWorkflow:
    @workflow.run
    async def run(self, call: ModelCall) -> list[str]:
        model = workflow.start_activity(
            stream_model_output, call, start_to_close_timeout=timedelta(seconds=60)
        )
        # The start of the Activity goes to the server when this Workflow Task completes, and
        # a `take` keeps the task open. So the Workflow lets the task complete first.
        await workflow.sleep(0.01)
        seen = workflow.get_stream(StreamRef(id=call.seen_id), body_type=str)
        batches: list[int] = []
        text = ""
        async for chunk in model_chunks(call.chunks_id, batches):
            text += chunk
            if text == "Hello":
                await seen.append("the first chunks")
        await model
        return [text, " ".join(str(size) for size in batches)]


async def test_generator_over_take_reads_a_running_activity(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    call = ModelCall(f"stream-{uuid.uuid4()}", f"stream-{uuid.uuid4()}")
    await stream_client.create_stream(id=call.chunks_id, body_type=str)
    await stream_client.create_stream(id=call.seen_id, body_type=str)
    async with new_worker(
        stream_client, LiveModelWorkflow, activities=[stream_model_output]
    ) as worker:
        handle = await stream_client.start_workflow(
            LiveModelWorkflow.run,
            call,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        text, batches = await handle.result()
    assert text == "Hello, world"
    # The first two chunks came in one `take`, and the last two in a later one.
    assert batches == "2 2"
    # One marker for each `take`, and one for the append of the Workflow.
    markers = _markers(await handle.fetch_history())
    assert len(markers) >= 3
    assert set(markers) == {"core_local_activity"}
