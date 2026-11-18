from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest

from temporalio import workflow
from temporalio.api.enums.v1 import EventType, StreamErrorReason
from temporalio.api.history.v1 import HistoryEvent
from temporalio.bridge.proto.external_data import StreamAppendEntry, StreamMarkerData
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.streams import BufferedAppender
from temporalio.exceptions import StreamError
from temporalio.nexus.system.stream_service import StreamRef
from temporalio.nexus.system.stream_service.workflow import create_stream, get_stream
from temporalio.testing import MemoryStreamStore
from temporalio.worker import Replayer
from tests.helpers import new_worker
from tests.nexus.test_stream_store import _with_store

_BURST = [f"record-{index}" for index in range(40)]


async def _yield_a_few_times() -> None:
    # A turn of the loop with no timer, so the write starts and waits for Core.
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
            # The write of "a" waits for Core, so these go into the next one.
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


def _append_entries(events: list[HistoryEvent]) -> list[StreamAppendEntry]:
    # Each append that Core makes is one entry in the Stream marker of its Workflow Task.
    entries: list[StreamAppendEntry] = []
    for event in events:
        attributes = event.marker_recorded_event_attributes
        if (
            event.event_type != EventType.EVENT_TYPE_MARKER_RECORDED
            or attributes.marker_name != "core_stream"
        ):
            continue
        [payload] = attributes.details["data"].payloads
        entries.extend(StreamMarkerData.FromString(payload.data).appends)
    return entries


async def _batches(
    client: Client, stream_id: str, events: list[HistoryEvent]
) -> list[int]:
    """Give the number of Records of each append, from its offsets in the marker."""
    stream = client.get_stream(StreamRef(id=stream_id), body_type=str)
    offsets = [record.offset async for record in stream.read()]
    return [
        sum(
            1 for offset in offsets if entry.first_offset <= offset <= entry.last_offset
        )
        for entry in _append_entries(events)
    ]


def _marker_names(events: list[HistoryEvent]) -> list[str]:
    return [
        event.marker_recorded_event_attributes.marker_name
        for event in events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    ]


async def _bodies(client: Client, stream_id: str) -> list[str]:
    stream = client.get_stream(StreamRef(id=stream_id), body_type=str)
    return [record.body async for record in stream.read()]


async def test_appends_of_one_activation_are_one_append(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    handle, last_offset = await _run(stream_client, BurstWorkflow, stream_id)

    events = list((await handle.fetch_history()).events)
    assert await _batches(stream_client, stream_id, events) == [40]
    assert await _bodies(stream_client, stream_id) == _BURST
    described = await stream_client.get_stream(
        StreamRef(id=stream_id), body_type=str
    ).describe()
    assert described.last_offset is not None
    assert last_offset == described.last_offset.hex()
    # The Local Activity of the create, and the Stream marker of the append.
    assert _marker_names(events) == ["core_local_activity", "core_stream"]
    assert _of_type(events, EventType.EVENT_TYPE_TIMER_STARTED) == 0


async def test_appends_during_a_workflow_append_join_the_next_one(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    handle, _ = await _run(stream_client, InFlightWorkflow, stream_id)

    events = list((await handle.fetch_history()).events)
    assert await _batches(stream_client, stream_id, events) == [1, 3]
    assert await _bodies(stream_client, stream_id) == ["a", "b", "c", "d"]
    assert _of_type(events, EventType.EVENT_TYPE_TIMER_STARTED) == 0


async def test_a_failed_workflow_append_stops_the_appender(client: Client):
    handle, result = await _run(
        _with_store(client, MemoryStreamStore()),
        FailedAppendWorkflow,
        f"stream-{uuid.uuid4()}",
    )
    assert result == ["STREAM_ERROR_REASON_NOT_FOUND", True, 3]
    # Only the first batch went to the Store, and it failed.
    [entry] = _append_entries(list((await handle.fetch_history()).events))
    assert entry.HasField("failure")


async def test_replay_of_workflow_appender_writes_nothing(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    handle, _ = await _run(stream_client, InFlightWorkflow, stream_id)

    # A replay of an append that Core made needs a Store that runs on the bridge, as the
    # first run had. A new memory Store shows any write, since it has no Stream.
    replay_store = MemoryStreamStore()
    await Replayer(
        workflows=[InFlightWorkflow],
        stream_config=_with_store(client, replay_store).config()["stream_config"],
    ).replay_workflow(await handle.fetch_history())
    with pytest.raises(StreamError) as err:
        await (
            _with_store(client, replay_store)
            .get_stream(StreamRef(id=stream_id), body_type=str)
            .describe()
        )
    assert err.value.reason == StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND
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
