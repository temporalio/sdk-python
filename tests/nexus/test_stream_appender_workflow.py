from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from google.protobuf.message import Message

import temporalio.api.streamservice.v1
from temporalio import workflow
from temporalio.api.enums.v1 import EventType, StreamErrorReason
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import BufferedAppender, Client, WorkflowHandle
from temporalio.exceptions import StreamError
from temporalio.nexus.system.stream_service import StreamRef
from temporalio.nexus.system.stream_service.workflow import create_stream, get_stream
from temporalio.testing import MemoryStreamStore
from temporalio.worker import Replayer
from tests.helpers import new_worker
from tests.helpers.streams import RecordingStreamStore
from tests.nexus.test_stream_store import _with_store

_BURST = [f"record-{index}" for index in range(40)]


class BatchStore(RecordingStreamStore):
    """Keeps the number of Records in each append."""

    def __init__(self) -> None:
        super().__init__(MemoryStreamStore())
        self.batches: list[int] = []

    async def before_call(self, operation: str, request: Message) -> None:
        if isinstance(request, temporalio.api.streamservice.v1.AppendRecordsRequest):
            self.batches.append(len(request.records))


async def _yield_a_few_times() -> None:
    # A turn of the loop with no timer, so the write starts and waits for its Local Activity.
    for _ in range(5):
        await asyncio.sleep(0)


@workflow.defn
class BurstWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = await create_stream(id=stream_id, body_type=str)
        appender = BufferedAppender(stream)
        for value in _BURST:
            appender.append(value)
        last_offset = await appender.flush()
        assert last_offset is not None
        return last_offset.hex()


@workflow.defn
class InFlightWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        stream = await create_stream(id=stream_id, body_type=str)
        async with BufferedAppender(stream) as appender:
            appender.append("a")
            await _yield_a_few_times()
            # The write of "a" waits for its Local Activity, so these go into the next one.
            appender.append("b")
            appender.append("c", "d")


@workflow.defn
class FailedAppendWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[Any]:
        appender = BufferedAppender(get_stream(StreamRef(id=stream_id), body_type=str))
        appender.append("a")
        await _yield_a_few_times()
        appender.append("b", "c")
        try:
            await appender.flush()
        except StreamError as err:
            first = err
        else:
            return ["flushed"]
        try:
            appender.append("d")
        except StreamError as err:
            later = err
        else:
            return ["appended"]
        return [
            StreamErrorReason.Name(first.reason),
            later is first,
            appender.unwritten,
        ]


@workflow.defn
class MaxDelayWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = get_stream(StreamRef(id=stream_id), body_type=str)
        try:
            BufferedAppender(stream, max_delay=timedelta(milliseconds=10))
        except ValueError as err:
            return str(err)
        return "built"


async def _run(
    client: Client, workflow_type: type[Any], stream_id: str
) -> tuple[WorkflowHandle[Any, Any], Any]:
    async with new_worker(client, workflow_type) as worker:
        handle: WorkflowHandle[Any, Any] = await client.start_workflow(
            workflow_type.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        return handle, await handle.result()


def _of_type(events: list[HistoryEvent], event_type: EventType.ValueType) -> int:
    return sum(1 for event in events if event.event_type == event_type)


async def _bodies(client: Client, stream_id: str) -> list[str]:
    stream = client.get_stream(StreamRef(id=stream_id), body_type=str)
    return [record.body async for record in stream.iter_read_items()]


async def test_appends_of_one_activation_are_one_append(client: Client):
    store = BatchStore()
    stream_client = _with_store(client, store)
    stream_id = f"stream-{uuid.uuid4()}"
    handle, last_offset = await _run(stream_client, BurstWorkflow, stream_id)

    assert store.batches == [40]
    assert await _bodies(stream_client, stream_id) == _BURST
    described = await stream_client.get_stream(
        StreamRef(id=stream_id), body_type=str
    ).describe()
    assert described.last_offset is not None
    assert last_offset == described.last_offset.hex()
    events = list((await handle.fetch_history()).events)
    # One marker for the create and one for the append.
    assert _of_type(events, EventType.EVENT_TYPE_MARKER_RECORDED) == 2
    assert _of_type(events, EventType.EVENT_TYPE_TIMER_STARTED) == 0


async def test_appends_during_a_workflow_append_join_the_next_one(client: Client):
    store = BatchStore()
    stream_client = _with_store(client, store)
    stream_id = f"stream-{uuid.uuid4()}"
    handle, _ = await _run(stream_client, InFlightWorkflow, stream_id)

    assert store.batches == [1, 3]
    assert await _bodies(stream_client, stream_id) == ["a", "b", "c", "d"]
    events = list((await handle.fetch_history()).events)
    assert _of_type(events, EventType.EVENT_TYPE_MARKER_RECORDED) == 3
    assert _of_type(events, EventType.EVENT_TYPE_TIMER_STARTED) == 0


async def test_a_failed_workflow_append_stops_the_appender(client: Client):
    store = BatchStore()
    _, result = await _run(
        _with_store(client, store), FailedAppendWorkflow, f"stream-{uuid.uuid4()}"
    )
    assert result == ["STREAM_ERROR_REASON_NOT_FOUND", True, 3]
    assert store.batches == [1]


async def test_replay_of_workflow_appender_writes_nothing(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    handle, _ = await _run(stream_client, InFlightWorkflow, stream_id)

    replay_store = BatchStore()
    await Replayer(
        workflows=[InFlightWorkflow],
        stream_config=_with_store(client, replay_store).config()["stream_config"],
    ).replay_workflow(await handle.fetch_history())
    assert replay_store.operations == []
    assert await _bodies(stream_client, stream_id) == ["a", "b", "c", "d"]


async def test_workflow_appender_refuses_a_max_delay(client: Client):
    _, message = await _run(
        _with_store(client, MemoryStreamStore()),
        MaxDelayWorkflow,
        f"stream-{uuid.uuid4()}",
    )
    assert message == (
        "BufferedAppender takes no max_delay in Workflow code, since a delay would put "
        "a timer into History. Leave max_delay as None."
    )
