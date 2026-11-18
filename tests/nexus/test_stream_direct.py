"""Reads and appends of a Stream that Core makes inside the Workflow Task."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload
from temporalio.api.enums.v1 import EventType, StreamErrorReason
from temporalio.api.streamservice.v1 import (
    AppendRecordsRequest,
    AppendRecordsResponse,
    ReadRecordsRequest,
    ReadRecordsResponse,
)
from temporalio.bridge.proto.external_data import (
    StreamCloseEntry,
    StreamMarkerData,
    StreamReadEntry,
)
from temporalio.client import (
    Client,
    StreamConfig,
    StreamReplaySource,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowHistory,
)
from temporalio.common import RawValue
from temporalio.converter import (
    DataConverter,
    PayloadCodec,
    SerializationContext,
    WithSerializationContext,
    WorkflowSerializationContext,
)
from temporalio.exceptions import ApplicationError, StreamError, StreamReplayError
from temporalio.nexus.system import stream_service
from temporalio.nexus.system.stream_service import (
    Producer,
    ProducerKindWorkflow,
    StreamRecord,
    StreamRef,
    StreamStatus,
)
from temporalio.runtime import MetricBuffer, Runtime, TelemetryConfig
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    Interceptor,
    Replayer,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)
from tests.helpers import assert_eventually, assert_task_fail_eventually, new_worker
from tests.helpers.streams import DictStreamStore, RecordingStreamStore
from tests.test_converter import SimpleCodec

# A Record body that a test can look for in the bytes of History.
_MARK = "direct-read-mark"


def _stream_client(
    client: Client,
    store: Any = None,
    *,
    replay_from: StreamReplaySource = StreamReplaySource.STORE,
    codec: bool = False,
) -> Client:
    config = client.config()
    config["stream_config"] = StreamConfig(
        stores=[store if store is not None else DictStreamStore()],
        workflow_read_replay_from=replay_from,
    )
    if codec:
        config["data_converter"] = DataConverter(payload_codec=SimpleCodec())
    return Client(**config)


def _markers(history: WorkflowHistory) -> list[str]:
    return [
        event.marker_recorded_event_attributes.marker_name
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    ]


def _stream_markers_per_task(history: WorkflowHistory) -> list[int]:
    """Give the number of `core_stream` markers that each completed Workflow Task wrote."""
    counts: list[int] = []
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED:
            counts.append(0)
        elif (
            event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
            and event.marker_recorded_event_attributes.marker_name == "core_stream"
        ):
            counts[-1] += 1
    return counts


def _completed_tasks(history: WorkflowHistory) -> int:
    return sum(
        1
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
    )


async def _filled_stream(
    client: Client, bodies: list[str], *, close: bool = True
) -> StreamRef[str]:
    stream = await client.create_stream(id=f"stream-{uuid.uuid4()}", body_type=str)
    if bodies:
        await stream.append(*bodies)
    if close:
        await stream.close()
    return stream.ref


async def _read_all(client: Client, ref: StreamRef[str]) -> list[StreamRecord[str]]:
    stream = client.get_stream(ref, body_type=str)
    return [record async for record in stream.read()]


async def _run(
    client: Client,
    workflow_type: type[Any],
    *args: Any,
    activities: list[Any] | None = None,
    max_cached_workflows: int = 1000,
    task_timeout: timedelta | None = None,
) -> tuple[WorkflowHandle[Any, Any], Any]:
    async with new_worker(
        client,
        workflow_type,
        activities=activities or [],
        max_cached_workflows=max_cached_workflows,
    ) as worker:
        handle: WorkflowHandle[Any, Any] = await client.start_workflow(
            workflow_type.run,
            args=list(args),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            task_timeout=task_timeout,
        )
        return handle, await handle.result()


# A generator over `read` that reads the Stream of an Activity that still runs.


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
    # The Activity writes the rest only after the Workflow says that it has the first
    # chunks. So the test passes only if Workflow code gets chunks while the Activity runs.
    told = False
    async for _ in seen.read(wait=timedelta(seconds=30)):
        told = True
        break
    if not told:
        raise ApplicationError(
            "The Workflow got no chunk while the Activity ran", non_retryable=True
        )
    await chunks.append(", ", "world")
    await chunks.close()


async def model_chunks(chunks_id: str) -> AsyncGenerator[str, None]:
    """Yield each chunk of the model's Stream, as the model stub of a library does."""
    stream = workflow.get_stream(StreamRef(id=chunks_id), body_type=str)
    # The wait is far longer than a pause of the model, so no page comes back empty.
    async for record in stream.read(wait=timedelta(seconds=30)):
        yield record.body


@workflow.defn
class LiveModelWorkflow:
    @workflow.run
    async def run(self, call: ModelCall) -> str:
        model = workflow.start_activity(
            stream_model_output, call, start_to_close_timeout=timedelta(seconds=60)
        )
        seen = workflow.get_stream(StreamRef(id=call.seen_id), body_type=str)
        text = ""
        async for chunk in model_chunks(call.chunks_id):
            text += chunk
            if text == "Hello":
                await seen.append("the first chunks")
        await model
        return text


async def test_generator_over_read_reads_a_running_activity(
    client: Client,
):
    stream_client = _stream_client(client)
    chunks = await _filled_stream(stream_client, [], close=False)
    seen = await _filled_stream(stream_client, [], close=False)
    handle, text = await _run(
        stream_client,
        LiveModelWorkflow,
        ModelCall(chunks.id, seen.id),
        activities=[stream_model_output],
    )
    assert text == "Hello, world"
    history = await handle.fetch_history()
    # The reads and the append write no Local Activity marker, and at most one marker for
    # each Workflow Task.
    assert "core_local_activity" not in _markers(history)
    assert "core_stream" in _markers(history)
    assert max(_stream_markers_per_task(history)) == 1


# The pages of a read.


@dataclass
class ReadArgs:
    stream_id: str
    from_offset: str | None = None
    wait_ms: int | None = None
    max_records: int | None = None
    max_bytes: int | None = None
    store: str = "dict"
    pages: int | None = None
    """The most pages to read. ``None`` reads until the iterator ends."""


@dataclass
class ReadResult:
    bodies: list[str]
    offsets: list[str]
    page_sizes: list[int]
    last_offsets: list[str | None]
    dones: list[bool]


def _ms(value: int | None) -> timedelta | None:
    return None if value is None else timedelta(milliseconds=value)


def _hex(value: bytes | None) -> str | None:
    return None if value is None else value.hex()


@workflow.defn
class ReadPagesWorkflow:
    @workflow.run
    async def run(self, args: ReadArgs) -> ReadResult:
        # The ref has its Store, so a replay with no Stream config knows it.
        ref = StreamRef[str](id=args.stream_id, store=args.store)
        stream = workflow.get_stream(ref, body_type=str)
        result = ReadResult([], [], [], [], [])
        async for page in stream.read_pages(
            from_offset=(
                bytes.fromhex(args.from_offset)
                if args.from_offset is not None
                else None
            ),
            max_records=args.max_records,
            wait=_ms(args.wait_ms),
            max_bytes=args.max_bytes,
        ):
            result.bodies.extend(record.body for record in page.records)
            result.offsets.extend(record.offset.hex() for record in page.records)
            result.page_sizes.append(len(page.records))
            result.last_offsets.append(_hex(page.last_offset))
            result.dones.append(page.done)
            if len(result.page_sizes) == args.pages:
                break
        return result


async def _read_pages(
    client: Client, args: ReadArgs, *, max_cached_workflows: int = 1000
) -> ReadResult:
    _, result = await _run(
        client, ReadPagesWorkflow, args, max_cached_workflows=max_cached_workflows
    )
    return result


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_closed_stream_ends_the_iterator_with_a_done_page(
    client: Client, max_cached_workflows: int
):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b", "c"])
    result = await _read_pages(
        stream_client,
        ReadArgs(ref.id, wait_ms=30_000),
        max_cached_workflows=max_cached_workflows,
    )
    assert result.bodies == ["a", "b", "c"]
    # The iterator ends after the page with `done`, also with a `wait`.
    assert result.dones[-1] is True
    assert not any(result.dones[:-1])
    assert result.last_offsets[-1] == result.offsets[-1]


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_read_with_max_records_gives_pages_of_two_two_and_one(
    client: Client, max_cached_workflows: int
):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b", "c", "d", "e"])
    result = await _read_pages(
        stream_client,
        ReadArgs(ref.id, max_records=2),
        max_cached_workflows=max_cached_workflows,
    )
    assert result.bodies == ["a", "b", "c", "d", "e"]
    assert result.page_sizes == [2, 2, 1]
    assert result.dones == [False, False, True]
    # Each page ends at its last Record, and the next one starts after it.
    assert result.last_offsets == [
        result.offsets[1],
        result.offsets[3],
        result.offsets[4],
    ]


@workflow.defn
class ReadOnePageWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str], wait_ms: int | None) -> list[Any]:
        stream = workflow.get_stream(ref, body_type=str)
        page = await stream.read_page(wait=_ms(wait_ms))
        return [[record.body for record in page.records], page.done, page.last_offset]


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_read_with_wait_on_a_quiet_stream_gives_an_empty_page(
    client: Client, max_cached_workflows: int
):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    handle, result = await _run(
        stream_client,
        ReadOnePageWorkflow,
        ref,
        1000,
        max_cached_workflows=max_cached_workflows,
    )
    assert result == [[], False, None]
    # The page comes only after the wait.
    events = (await handle.fetch_history()).events
    elapsed = events[-1].event_time.ToDatetime() - events[0].event_time.ToDatetime()
    assert elapsed >= timedelta(seconds=1)


async def test_read_with_no_wait_on_an_empty_stream_gives_an_empty_page(
    client: Client,
):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    handle, result = await _run(stream_client, ReadOnePageWorkflow, ref, None)
    assert result == [[], False, None]
    events = (await handle.fetch_history()).events
    # An empty Stream gives no reason to wait.
    took = events[-1].event_time.ToDatetime() - events[0].event_time.ToDatetime()
    assert took < timedelta(seconds=5)


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_read_with_no_wait_gives_what_is_there_and_ends(
    client: Client, max_cached_workflows: int
):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b", "c"], close=False)
    handle, bodies = await _run(
        stream_client,
        ReadItemsWorkflow,
        ref,
        None,
        max_cached_workflows=max_cached_workflows,
    )
    assert bodies == ["a", "b", "c"]
    events = (await handle.fetch_history()).events
    # The read doesn't wait for the Store, so the run ends long before the first heartbeat.
    took = events[-1].event_time.ToDatetime() - events[0].event_time.ToDatetime()
    assert took < timedelta(seconds=5)


@workflow.defn
class ReadItemsWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str], wait_ms: int | None) -> list[str]:
        stream = workflow.get_stream(ref, body_type=str)
        return [record.body async for record in stream.read(wait=_ms(wait_ms))]


async def test_page_that_takes_the_last_record_with_a_cap_is_done(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b", "c"])
    # The cap takes the last Record of the closed Stream, so the page is done.
    by_records = await _read_pages(stream_client, ReadArgs(ref.id, max_records=3))
    assert (by_records.page_sizes, by_records.dones) == ([3], [True])
    one = await _filled_stream(stream_client, ["a"])
    by_bytes = await _read_pages(stream_client, ReadArgs(one.id, max_bytes=1))
    assert (by_bytes.page_sizes, by_bytes.dones) == ([1], [True])
    # On an open Stream the same page isn't done.
    open_ref = await _filled_stream(stream_client, ["a", "b", "c"], close=False)
    capped = await _read_pages(
        stream_client, ReadArgs(open_ref.id, max_records=3, pages=1)
    )
    assert (capped.page_sizes, capped.dones) == ([3], [False])


async def test_page_with_max_bytes_gives_the_first_record_over_the_cap(
    client: Client,
):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b", "c"])
    result = await _read_pages(stream_client, ReadArgs(ref.id, max_bytes=1))
    # The first Record of each page always comes, even over the cap.
    assert result.page_sizes == [1, 1, 1]
    assert result.dones == [False, False, True]


@activity.defn
async def append_after_a_quiet_gap(stream_id: str) -> None:
    stream = activity.client().get_stream(StreamRef(id=stream_id), body_type=str)
    await stream.append("a")
    await asyncio.sleep(2)
    await stream.append("b")
    await stream.close()


@workflow.defn
class QuietGapWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        writer = workflow.start_activity(
            append_after_a_quiet_gap,
            stream_id,
            start_to_close_timeout=timedelta(seconds=30),
        )
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        got = [record.body async for record in stream.read(wait=timedelta(seconds=10))]
        await writer
        return got


async def test_read_with_a_wait_reads_across_a_quiet_gap(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    handle, result = await _run(
        stream_client,
        QuietGapWorkflow,
        ref.id,
        activities=[append_after_a_quiet_gap],
    )
    assert result == ["a", "b"]
    # The gap of 2 s is shorter than the wait, so one Core read gets both Records.
    assert len(_read_seqs(await handle.fetch_history())) == 1


# A loop with no await in its body, and one that awaits for each Record.


def _read_entries(history: WorkflowHistory) -> list[list[StreamReadEntry]]:
    """Give the read entries of the `core_stream` marker of each Workflow Task."""
    entries: list[list[StreamReadEntry]] = []
    for event in history.events:
        attributes = event.marker_recorded_event_attributes
        if (
            event.event_type != EventType.EVENT_TYPE_MARKER_RECORDED
            or attributes.marker_name != "core_stream"
        ):
            continue
        [payload] = attributes.details["data"].payloads
        entries.append(list(StreamMarkerData.FromString(payload.data).reads))
    return entries


def _read_seqs(history: WorkflowHistory) -> set[int]:
    """Give the seq of each Core read that History has."""
    return {entry.seq for task in _read_entries(history) for entry in task}


@activity.defn
async def append_five_slowly(stream_id: str) -> None:
    stream = activity.client().get_stream(StreamRef(id=stream_id), body_type=str)
    for index in range(5):
        await asyncio.sleep(0.3)
        await stream.append(f"record-{index}")
    await stream.close()


@workflow.defn
class LoopWithNoAwaitWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        writer = workflow.start_activity(
            append_five_slowly, stream_id, start_to_close_timeout=timedelta(seconds=30)
        )
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        got = []
        async for record in stream.read(wait=timedelta(seconds=10)):
            got.append(record.body)
        await writer
        return got


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_loop_with_no_await_keeps_one_core_read(
    client: Client, max_cached_workflows: int
):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    handle, got = await _run(
        stream_client,
        LoopWithNoAwaitWorkflow,
        ref.id,
        activities=[append_five_slowly],
        max_cached_workflows=max_cached_workflows,
    )
    assert got == [f"record-{index}" for index in range(5)]
    entries = _read_entries(await handle.fetch_history())
    # The loop asks for the next page in the activation of the last one, so one Core
    # read gives every page.
    assert len({entry.seq for task in entries for entry in task}) == 1
    assert any(len(task) == 1 and len(task[0].page_counts) > 1 for task in entries)


@workflow.defn
class LoopWithAppendWorkflow:
    @workflow.run
    async def run(self, stream_id: str, out_id: str) -> list[str]:
        writer = workflow.start_activity(
            append_five_slowly, stream_id, start_to_close_timeout=timedelta(seconds=30)
        )
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        out = workflow.get_stream(StreamRef(id=out_id), body_type=str)
        got = []
        async for record in stream.read(wait=timedelta(seconds=10)):
            got.append(record.body)
            await out.append(record.body)
        await writer
        return got


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_loop_that_awaits_an_append_closes_and_starts_the_read(
    client: Client, max_cached_workflows: int
):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    out_ref = await _filled_stream(stream_client, [], close=False)
    handle, got = await _run(
        stream_client,
        LoopWithAppendWorkflow,
        ref.id,
        out_ref.id,
        activities=[append_five_slowly],
        max_cached_workflows=max_cached_workflows,
    )
    bodies = [f"record-{index}" for index in range(5)]
    assert got == bodies
    # Each Record went to the other Stream once and in order, whatever the replays.
    assert [record.body for record in await _read_all(stream_client, out_ref)] == bodies
    # The append ends the activation with no call waiting on the read, so the read
    # closes, and the next page starts another one.
    assert len(_read_seqs(await handle.fetch_history())) > 1


# The example of the design review:
#
#     let mut i = 0;
#     while let item = stream.next() {
#         workflow.spawn(loop { yield_once.await; i++; })
#         if i == 2 { await execute_activity(...); }
#     }
#
# Each Record starts one more task that counts the wakes of the Workflow. So whether the
# Activity starts, and at which Record, depends on the activations that gave the Workflow
# its Records, and not on the Records.


class _Wakes:
    """Lets a task of a Workflow wait for the next wake of the Workflow.

    Python has no call for it. ``asyncio.sleep(0)`` resumes in the same activation, so a
    loop of it never lets the activation end.
    """

    def __init__(self) -> None:
        self._new: list[asyncio.Future[None]] = []
        self._parked: list[asyncio.Future[None]] = []
        self._resumed = False
        self._watch = asyncio.create_task(workflow.wait_condition(self._on_idle))

    def _on_idle(self) -> bool:
        # The event loop checks a condition each time it has no ready task. That is once at
        # the end of a wake, and once more when the tasks that this resumed have run. The
        # second check is in the same wake, so it resumes nothing.
        if self._resumed:
            self._resumed = False
            self._parked += self._new
        else:
            self._resumed = bool(self._parked)
            for future in self._parked:
                if not future.done():
                    future.set_result(None)
            self._parked = self._new
        self._new = []
        return False

    async def yield_once(self) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._new.append(future)
        await future


@workflow.defn
class CounterTasksWorkflow:
    def __init__(self) -> None:
        self.i = 0

    async def count(self, wakes: _Wakes) -> None:
        while True:
            await wakes.yield_once()
            self.i += 1

    @workflow.run
    async def run(self, stream_id: str) -> list[int]:
        wakes = _Wakes()
        seen: list[int] = []
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        async for item in stream.read(wait=timedelta(seconds=30)):
            asyncio.create_task(self.count(wakes))
            seen.append(self.i)
            if self.i == 2:
                await workflow.execute_activity(
                    tag, item.body, start_to_close_timeout=timedelta(seconds=10)
                )
        return seen


async def _read_calls(count: int) -> None:
    """Wait until Workflow code has made this many `read_page` calls."""

    async def check() -> None:
        assert [name for name, _ in _intercepted].count("ReadRecords") >= count

    await assert_eventually(check, interval=timedelta(milliseconds=20))


async def _run_counter_tasks(
    client: Client, pages: list[list[str]], *, max_cached_workflows: int = 1000
) -> tuple[list[int], WorkflowHistory]:
    """Run the example, and give it each page of Records in an activation of its own.

    The last page must make the Workflow start its Activity.
    """
    _intercepted.clear()
    stream = await client.create_stream(id=f"stream-{uuid.uuid4()}", body_type=str)
    # The Records that the Stream has when the read starts come in one page.
    await stream.append(*pages[0])
    async with new_worker(
        client,
        CounterTasksWorkflow,
        activities=[tag],
        interceptors=[StreamCallInterceptor()],
        max_cached_workflows=max_cached_workflows,
    ) as worker:
        handle = await client.start_workflow(
            CounterTasksWorkflow.run,
            stream.ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        for calls, page in enumerate(pages[1:], start=2):
            # The loop asks for its next page in the activation that gave it the page
            # before. A Record that comes after that call is in a later activation.
            await _read_calls(calls)
            await stream.append(*page)

        async def activity_started() -> None:
            assert _scheduled_activities(await handle.fetch_history()) == ["tag"]

        await assert_eventually(activity_started)
        await stream.close()
        seen = await handle.result()
    return seen, await handle.fetch_history()


_COUNTER_PAGES = [["record-0", "record-1"], ["record-2"], ["record-3"]]


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_counter_tasks_start_the_activity_by_the_activations(
    client: Client, max_cached_workflows: int
):
    stream_client = _stream_client(client)
    seen, history = await _run_counter_tasks(
        stream_client, _COUNTER_PAGES, max_cached_workflows=max_cached_workflows
    )
    # The first two Records start a task each, and both tasks count the next wake. The loop
    # reads `i` before the tasks of a wake run, so `i` is 2 at the fourth Record.
    assert seen == [0, 0, 0, 2]
    # The marker of the Workflow Task that started the Activity has the three pages, and
    # each page is in an activation of its own.
    [entry] = _read_entries(history)[0]
    assert list(entry.page_counts) == [2, 1, 1]
    assert all(gap > 0 for gap in entry.page_activations[1:])
    # A replay gives each page in the activation that got it, so `i` is the same at each
    # Record. With no cache, the Workflow Tasks after the first one were such replays too.
    config = stream_client.config()["stream_config"]
    await Replayer(
        workflows=[CounterTasksWorkflow], stream_config=config
    ).replay_workflow(history)


async def test_counter_tasks_with_one_page_start_no_activity(client: Client):
    stream_client = _stream_client(client)
    bodies = [body for page in _COUNTER_PAGES for body in page]
    ref = await _filled_stream(stream_client, bodies)
    handle, seen = await _run(
        stream_client, CounterTasksWorkflow, ref.id, activities=[tag]
    )
    # With the same Records in one activation, no task counts a wake between two of them.
    assert seen == [0, 0, 0, 0]
    assert _scheduled_activities(await handle.fetch_history()) == []


async def test_replay_with_other_activations_is_a_nondeterminism_error(client: Client):
    stream_client = _stream_client(client)
    seen, history = await _run_counter_tasks(stream_client, _COUNTER_PAGES)
    assert seen == [0, 0, 0, 2]
    # Put every page of a read into the activation of its first page. `i` then stays 0, and
    # the replay does not start the Activity that History has.
    for event in history.events:
        attributes = event.marker_recorded_event_attributes
        if (
            event.event_type != EventType.EVENT_TYPE_MARKER_RECORDED
            or attributes.marker_name != "core_stream"
        ):
            continue
        [payload] = attributes.details["data"].payloads
        data = StreamMarkerData.FromString(payload.data)
        for entry in data.reads:
            gaps = [0] * (len(entry.page_activations) - 1)
            del entry.page_activations[1:]
            entry.page_activations.extend(gaps)
        payload.data = data.SerializeToString()
    config = stream_client.config()["stream_config"]
    replay = await Replayer(
        workflows=[CounterTasksWorkflow], stream_config=config
    ).replay_workflow(history, raise_on_replay_failure=False)
    assert isinstance(replay.replay_failure, workflow.NondeterminismError)


# The Records of one page, taken in several activations. Core records the page
# when it gives it. An Activity between two Records is a later Workflow Task,
# and that task has no marker.


@workflow.defn
class ActivityForEachRecordWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        got = []
        async for item in stream.read():
            got.append(
                await workflow.execute_activity(
                    tag, item.body, start_to_close_timeout=timedelta(seconds=10)
                )
            )
        return got


@workflow.defn
class OneRecordPerReadWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        got = []
        offset = None
        while True:
            page = await stream.read_page(from_offset=offset, max_records=1)
            for record in page.records:
                got.append(
                    await workflow.execute_activity(
                        tag,
                        record.body,
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                )
            offset = page.last_offset
            if page.done or not page.records:
                return got


async def _one_page_across_activations(
    client: Client, workflow_type: type[Any], *, max_cached_workflows: int
) -> None:
    ref = await _filled_stream(client, ["a", "b", "c", "d"])
    handle, got = await _run(
        client,
        workflow_type,
        ref.id,
        activities=[tag],
        max_cached_workflows=max_cached_workflows,
    )
    assert got == [f"tagged {body}" for body in "abcd"]
    history = await handle.fetch_history()
    # The last Activity's result is a task of its own, and that task completes
    # the Workflow. It records nothing, so the marker stays in the first task.
    assert _completed_tasks(history) == 5
    assert _scheduled_activities(history) == ["tag", "tag", "tag", "tag"]
    task = 0
    marker_task = None
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED:
            task += 1
        elif (
            event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
            and event.marker_recorded_event_attributes.marker_name == "core_stream"
        ):
            assert marker_task is None
            marker_task = task
    assert marker_task == 1
    entries = _read_entries(history)
    assert len(entries) == 1
    [entry] = entries[0]
    assert list(entry.page_counts) == [4]
    assert list(entry.page_activations) == [1]
    config = client.config()["stream_config"]
    await Replayer(workflows=[workflow_type], stream_config=config).replay_workflow(
        history
    )


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_one_page_across_activities_keeps_its_marker(
    client: Client, max_cached_workflows: int
):
    await _one_page_across_activations(
        _stream_client(client),
        ActivityForEachRecordWorkflow,
        max_cached_workflows=max_cached_workflows,
    )


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_one_record_reads_of_one_page_keep_its_marker(
    client: Client, max_cached_workflows: int
):
    await _one_page_across_activations(
        _stream_client(client),
        OneRecordPerReadWorkflow,
        max_cached_workflows=max_cached_workflows,
    )


# What `read_page` checks, and where it can't run.


@workflow.defn
class LongOffsetWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> list[str]:
        stream = workflow.get_stream(ref, body_type=str)
        got: list[str] = []
        try:
            await stream.read_page(from_offset=b"o" * 2000)
            got.append("page")
        except StreamError as err:
            got.append(StreamErrorReason.Name(err.reason))
        got.extend([record.body async for record in stream.read()])
        return got


async def test_read_from_an_offset_over_1024_bytes_raises_invalid(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b"])
    handle, got = await _run(stream_client, LongOffsetWorkflow, ref)
    assert got == ["STREAM_ERROR_REASON_INVALID", "a", "b"]
    config = stream_client.config()["stream_config"]
    await Replayer(
        workflows=[LongOffsetWorkflow], stream_config=config
    ).replay_workflow(history=await handle.fetch_history())


@workflow.defn
class QueryReadWorkflow:
    def __init__(self) -> None:
        self.ref: StreamRef[str] | None = None

    @workflow.run
    async def run(self, ref: StreamRef[str]) -> None:
        self.ref = ref
        await workflow.wait_condition(lambda: False)

    @workflow.query
    def start_read(self) -> str:
        assert self.ref is not None
        step = workflow.get_stream(self.ref, body_type=str).read_page()
        try:
            step.send(None)
        except Exception as err:
            return type(err).__name__
        finally:
            step.close()
        return "started"


async def test_read_in_a_query_raises_the_read_only_error(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a"])
    async with new_worker(stream_client, QueryReadWorkflow) as worker:
        handle = await stream_client.start_workflow(
            QueryReadWorkflow.run,
            ref,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.query(QueryReadWorkflow.start_read) == (
            "ReadOnlyContextError"
        )
        await handle.terminate()


# Reads that resume from an offset.


@workflow.defn
class LoopReadWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> list[str]:
        stream = workflow.get_stream(ref, body_type=str)
        got: list[str] = []
        # The first loop leaves with `break`, and its next request resumes there.
        items = stream.read(max_records=4)
        async for record in items:
            got.append(record.body)
            if len(got) == 3:
                break
        assert items.next_request is not None
        last = items.next_request.from_offset
        # The last reads go on from there to the end, two Records at a time.
        while True:
            page = await stream.read_page(from_offset=last, max_records=2)
            got.extend(record.body for record in page.records)
            last = page.last_offset
            if page.done:
                return got


async def test_reads_from_an_offset_get_each_record_once(client: Client):
    stream_client = _stream_client(client)
    bodies = [f"record-{index}" for index in range(10)]
    ref = await _filled_stream(stream_client, bodies)
    _, got = await _run(stream_client, LoopReadWorkflow, ref)
    assert got == bodies


# The marker in History.


async def _history_of_a_read(
    client: Client, replay_from: StreamReplaySource
) -> tuple[WorkflowHistory, StreamRef[str], Client]:
    stream_client = _stream_client(client, replay_from=replay_from)
    ref = await _filled_stream(stream_client, [f"{_MARK}-1", f"{_MARK}-2"])
    handle, result = await _run(stream_client, ReadPagesWorkflow, ReadArgs(ref.id))
    assert result.bodies == [f"{_MARK}-1", f"{_MARK}-2"]
    return await handle.fetch_history(), ref, stream_client


def _markers_have_mark(history: WorkflowHistory) -> bool:
    # The result of the Workflow holds the bodies, so only the markers count.
    return any(
        _MARK.encode() in event.SerializeToString()
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    )


async def test_store_marker_holds_no_body(client: Client):
    history, _, _ = await _history_of_a_read(client, StreamReplaySource.STORE)
    assert "core_stream" in _markers(history)
    assert not _markers_have_mark(history)


async def test_history_marker_holds_the_bodies(client: Client):
    history, _, _ = await _history_of_a_read(client, StreamReplaySource.HISTORY)
    assert "core_stream" in _markers(history)
    assert _markers_have_mark(history)


# Replay.


async def test_replay_of_store_marker_reads_the_store(client: Client):
    history, _, stream_client = await _history_of_a_read(
        client, StreamReplaySource.STORE
    )
    config = stream_client.config()["stream_config"]
    await Replayer(workflows=[ReadPagesWorkflow], stream_config=config).replay_workflow(
        history
    )


async def _failed_replay(
    history: WorkflowHistory, ref: StreamRef[str], config: StreamConfig | None
) -> str:
    """Replay with and without a raise, and give the text of the failure."""
    replayer = Replayer(workflows=[ReadPagesWorkflow], stream_config=config)
    with pytest.raises(RuntimeError) as err:
        await replayer.replay_workflow(history)
    result = await replayer.replay_workflow(history, raise_on_replay_failure=False)
    assert isinstance(result.replay_failure, RuntimeError)
    assert str(result.replay_failure) == str(err.value)
    message = str(err.value)
    assert f"StreamReplayError: Stream `{ref.id}` in Store `dict`" in message
    return message


async def test_replay_of_store_marker_with_no_stream_config_fails(client: Client):
    history, ref, _ = await _history_of_a_read(client, StreamReplaySource.STORE)
    message = await _failed_replay(history, ref, None)
    assert "this Worker has no Stream runtime for the Store `dict`" in message


async def test_replay_of_store_marker_fails_when_records_are_gone(client: Client):
    history, ref, _ = await _history_of_a_read(client, StreamReplaySource.STORE)
    # A new Store of the same name has none of the Records.
    message = await _failed_replay(
        history, ref, StreamConfig(stores=[DictStreamStore()])
    )
    assert "stream_not_found" in message


async def test_replay_of_history_marker_needs_no_stream_config(client: Client):
    history, _, _ = await _history_of_a_read(client, StreamReplaySource.HISTORY)
    await Replayer(workflows=[ReadPagesWorkflow]).replay_workflow(history)


# Two reads at once, on a Worker with no cache and on a cached one.


@activity.defn
async def tag(body: str) -> str:
    return f"tagged {body}"


@dataclass
class TwoReadsArgs:
    first_id: str
    second_id: str
    out_id: str


@dataclass
class TwoReadsResult:
    first: list[str]
    second: list[str]
    count: int
    reads: int


@workflow.defn
class TwoReadsWorkflow:
    def __init__(self) -> None:
        self.count = 0
        self.reads = 0

    async def _read(self, stream_id: str, out: Any) -> list[str]:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        got: list[str] = []
        last = None
        while True:
            self.reads += 1
            page = await stream.read_page(from_offset=last, max_records=2)
            for record in page.records:
                tagged = await workflow.execute_local_activity(
                    tag, record.body, start_to_close_timeout=timedelta(seconds=10)
                )
                await out.append(tagged)
                self.count += 1
                got.append(tagged)
            last = page.last_offset
            if page.done:
                return got
            # A timer ends the Workflow Task, so a Worker with no cache replays each read.
            await workflow.sleep(0.05)

    @workflow.run
    async def run(self, args: TwoReadsArgs) -> TwoReadsResult:
        out = workflow.get_stream(StreamRef(id=args.out_id), body_type=str)
        first, second = await asyncio.gather(
            self._read(args.first_id, out), self._read(args.second_id, out)
        )
        return TwoReadsResult(first, second, self.count, self.reads)


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_two_reads_with_and_without_a_cache(
    client: Client, max_cached_workflows: int
):
    stream_client = _stream_client(client)
    first = [f"first-{index}" for index in range(5)]
    second = [f"second-{index}" for index in range(5)]
    out_ref = await _filled_stream(stream_client, [], close=False)
    args = TwoReadsArgs(
        first_id=(await _filled_stream(stream_client, first)).id,
        second_id=(await _filled_stream(stream_client, second)).id,
        out_id=out_ref.id,
    )
    handle, result = await _run(
        stream_client,
        TwoReadsWorkflow,
        args,
        activities=[tag],
        max_cached_workflows=max_cached_workflows,
    )
    assert result == TwoReadsResult(
        first=[f"tagged {body}" for body in first],
        second=[f"tagged {body}" for body in second],
        count=10,
        reads=6,
    )
    # Each Record went to the out Stream once, whatever the replays.
    out = [record.body for record in await _read_all(stream_client, out_ref)]
    assert sorted(out) == sorted(result.first + result.second)
    assert _completed_tasks(await handle.fetch_history()) > 2


# A Direct append.


@workflow.defn
class AppendWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> list[str | None]:
        stream = workflow.get_stream(ref, body_type=str)
        appended = await stream.append("a", "b")
        # A timer ends the Workflow Task, so a Worker with no cache replays the append.
        await workflow.sleep(0.05)
        return [_hex(appended.first_offset), _hex(appended.last_offset)]


async def test_direct_append(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    handle, offsets = await _run(
        stream_client, AppendWorkflow, ref, max_cached_workflows=0
    )
    records = await _read_all(stream_client, ref)
    # The replay of the next Workflow Task appended nothing.
    assert [record.body for record in records] == ["a", "b"]
    assert offsets == [record.offset.hex() for record in records]
    history = await handle.fetch_history()
    run_id = handle.result_run_id
    started = [
        event.event_id
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED
    ]
    for record in records:
        producer = record.producer
        assert isinstance(producer, Producer)
        assert producer.id == f"wf:{run_id}"
        assert isinstance(producer.kind, ProducerKindWorkflow)
        assert producer.kind.value.run_id == run_id
        epoch = producer.kind.value.epoch
        assert epoch is not None
        # The epoch is the first Workflow Task, which made the append.
        assert (epoch.started_event_id, epoch.attempt) == (started[0], 1)
    assert _markers(history) == ["core_stream"]
    # A Replayer appends nothing either.
    await Replayer(
        workflows=[AppendWorkflow],
        stream_config=stream_client.config()["stream_config"],
    ).replay_workflow(history)
    assert len(await _read_all(stream_client, ref)) == 2


@workflow.defn
class RefusedAppendWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> list[str]:
        stream = workflow.get_stream(ref, body_type=str)
        try:
            await stream.append("a")
        except StreamError as err:
            # A timer only on the error path, so a replay without the error differs.
            await workflow.sleep(0.01)
            return [
                StreamErrorReason.Name(err.reason),
                err.message,
                str(err.non_retryable),
            ]
        return ["appended"]


async def test_refused_append_raises_stream_error_live_and_in_replay(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a"])
    handle, result = await _run(stream_client, RefusedAppendWorkflow, ref)
    # The client path gives the same error for the same append.
    with pytest.raises(StreamError) as client_err:
        await stream_client.get_stream(ref, body_type=str).append("a")
    assert result == [
        "STREAM_ERROR_REASON_CLOSED",
        client_err.value.message,
        "True",
    ]
    await Replayer(
        workflows=[RefusedAppendWorkflow],
        stream_config=stream_client.config()["stream_config"],
    ).replay_workflow(await handle.fetch_history())


# A Workflow Task that fails once in the middle of a read.

_fail_read_task = True


@workflow.defn(sandboxed=False)
class FailOnceInReadWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str], out_ref: StreamRef[str]) -> list[str]:
        stream = workflow.get_stream(ref, body_type=str)
        out = workflow.get_stream(out_ref, body_type=str)
        got: list[str] = []
        async for record in stream.read():
            got.append(record.body)
            if record.body == "record-2":
                await out.append("seen record-2")
                global _fail_read_task
                if _fail_read_task:
                    _fail_read_task = False
                    raise RuntimeError("Failed the Workflow Task once")
        return got


async def test_failed_task_in_a_read_gets_each_record_once(client: Client):
    global _fail_read_task
    _fail_read_task = True
    stream_client = _stream_client(client)
    bodies = [f"record-{index}" for index in range(5)]
    ref = await _filled_stream(stream_client, bodies)
    out_ref = await _filled_stream(stream_client, [], close=False)
    _, got = await _run(stream_client, FailOnceInReadWorkflow, ref, out_ref)
    assert not _fail_read_task
    assert got == bodies
    # The append of the failed task reached the Store, and its retry appends again.
    out = await _read_all(stream_client, out_ref)
    assert [record.body for record in out] == ["seen record-2"] * 2
    epochs = []
    for record in out:
        assert record.producer is not None
        assert isinstance(record.producer.kind, ProducerKindWorkflow)
        epoch = record.producer.kind.value.epoch
        assert epoch is not None
        epochs.append((epoch.started_event_id, epoch.attempt))
    assert epochs[0] < epochs[1]


# A Signal while a read waits.


@workflow.defn
class SignalStopsReadWorkflow:
    def __init__(self) -> None:
        self.signaled = False

    @workflow.run
    async def run(self, ref: StreamRef[str]) -> list[Any]:
        stream = workflow.get_stream(ref, body_type=str)
        got: list[str] = []

        async def read_all() -> None:
            async for record in stream.read(wait=timedelta(seconds=60)):
                got.append(record.body)

        reader = asyncio.create_task(read_all())
        await workflow.wait_condition(lambda: self.signaled)
        # The cancel leaves the read with no call that waits on it, so it closes.
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        return [got, self.signaled]

    @workflow.signal
    def stop(self) -> None:
        self.signaled = True


async def test_signal_reaches_a_waiting_read(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a"], close=False)
    async with new_worker(stream_client, SignalStopsReadWorkflow) as worker:
        handle = await stream_client.start_workflow(
            SignalStopsReadWorkflow.run,
            ref,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            # The heartbeat of an open task comes at 80 % of its timeout.
            task_timeout=timedelta(seconds=2),
        )
        await asyncio.sleep(0.5)
        await handle.signal(SignalStopsReadWorkflow.stop)
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result == [["a"], True]
    await Replayer(
        workflows=[SignalStopsReadWorkflow],
        stream_config=stream_client.config()["stream_config"],
    ).replay_workflow(await handle.fetch_history())


# A second loop after a `break`, and the ends of a run with a read open.


@activity.defn
async def append_and_close(stream_id: str) -> None:
    stream = activity.client().get_stream(StreamRef(id=stream_id), body_type=str)
    await stream.append("x", "y")
    await stream.close()


@workflow.defn
class BreakThenReadAgainWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[Any]:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        first = stream.read(wait=timedelta(seconds=30))
        got_first: list[str] = []
        async for record in first:
            got_first.append(record.body)
            if len(got_first) == 2:
                break
        assert first.next_request is not None
        # The second loop resumes after the last Record that the first one gave.
        second = stream.read_pages(
            from_offset=first.next_request.from_offset, wait=timedelta(seconds=30)
        )
        writer = workflow.start_activity(
            append_and_close, stream_id, start_to_close_timeout=timedelta(seconds=30)
        )
        got_second: list[str] = []
        done = False
        async for page in second:
            got_second.extend(record.body for record in page.records)
            done = page.done
        await writer
        return [got_first, got_second, done]


async def test_second_loop_after_a_break_gets_every_record(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b", "c"], close=False)
    handle, result = await _run(
        stream_client, BreakThenReadAgainWorkflow, ref.id, activities=[append_and_close]
    )
    assert result == [["a", "b"], ["c", "x", "y"], True]
    await Replayer(
        workflows=[BreakThenReadAgainWorkflow],
        stream_config=stream_client.config()["stream_config"],
    ).replay_workflow(await handle.fetch_history())


@workflow.defn
class CompleteWithOpenReadsWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        first = await stream.read_page(wait=timedelta(seconds=60))
        # A second read waits in its own task while the Workflow returns.
        waiting = asyncio.create_task(
            stream.read_page(from_offset=first.last_offset, wait=timedelta(seconds=60))
        )
        await asyncio.sleep(0)
        assert not waiting.done()
        return "completed"


async def test_workflow_completes_with_open_reads(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a"], close=False)
    async with new_worker(stream_client, CompleteWithOpenReadsWorkflow) as worker:
        handle = await stream_client.start_workflow(
            CompleteWithOpenReadsWorkflow.run,
            ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await asyncio.wait_for(handle.result(), timeout=20) == "completed"
    await Replayer(
        workflows=[CompleteWithOpenReadsWorkflow],
        stream_config=stream_client.config()["stream_config"],
    ).replay_workflow(await handle.fetch_history())


@workflow.defn
class CancelDuringReadWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[Any]:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        got: list[str] = []
        try:
            async for record in stream.read(wait=timedelta(seconds=60)):
                got.append(record.body)
        except asyncio.CancelledError:
            return [got, "cancelled"]
        return [got, "ended"]


async def test_cancel_reaches_a_waiting_read(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a"], close=False)
    async with new_worker(stream_client, CancelDuringReadWorkflow) as worker:
        handle = await stream_client.start_workflow(
            CancelDuringReadWorkflow.run,
            ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            # The heartbeat of an open task comes at 80 % of its timeout.
            task_timeout=timedelta(seconds=2),
        )
        await asyncio.sleep(0.5)
        await handle.cancel()
        result = await asyncio.wait_for(handle.result(), timeout=30)
    assert result == [["a"], "cancelled"]


@workflow.defn
class ContinueWithOpenReadWorkflow:
    @workflow.run
    async def run(self, stream_id: str, from_offset: str | None) -> list[str]:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        if from_offset is None:
            # The Core read of this page is still open when the run continues.
            first = await stream.read_page(max_records=1, wait=timedelta(seconds=60))
            assert first.last_offset is not None
            workflow.continue_as_new(args=[stream_id, first.last_offset.hex()])
        items = stream.read(from_offset=bytes.fromhex(from_offset))
        return [record.body async for record in items]


async def test_continue_as_new_with_an_open_read(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b", "c"], close=False)
    async with new_worker(stream_client, ContinueWithOpenReadWorkflow) as worker:
        handle = await stream_client.start_workflow(
            ContinueWithOpenReadWorkflow.run,
            args=[ref.id, None],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result == ["b", "c"]
    first_run = stream_client.get_workflow_handle(
        handle.id, run_id=handle.first_execution_run_id
    )
    await Replayer(
        workflows=[ContinueWithOpenReadWorkflow],
        stream_config=stream_client.config()["stream_config"],
    ).replay_workflow(await first_run.fetch_history())


# A Store written in Python.


@workflow.defn
class PythonStoreAppendCloseWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> None:
        stream = workflow.get_stream(ref, body_type=str)
        await stream.append("a", "b")
        await stream.close()


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_python_store_takes_a_workflow_append_and_close(
    client: Client, max_cached_workflows: int
):
    store = DictStreamStore()
    stream_client = _stream_client(client, store)
    ref = await _filled_stream(stream_client, [], close=False)
    assert ref.store == "dict"
    handle, _ = await _run(
        stream_client,
        PythonStoreAppendCloseWorkflow,
        ref,
        max_cached_workflows=max_cached_workflows,
    )
    assert [record.body for record in await _read_all(stream_client, ref)] == [
        "a",
        "b",
    ]
    assert await _status(stream_client, ref) == StreamStatus.StreamStatusClosed
    # The Store gets the Workflow's Producer with the Workflow Task as its epoch.
    producer = store.streams[ref.id].records[0].producer
    assert producer.WhichOneof("kind") == "workflow"
    assert producer.workflow.run_id == handle.result_run_id
    assert producer.workflow.epoch.started_event_id > 0
    # Core made the calls, with no Local Activity.
    history = await handle.fetch_history()
    assert "core_local_activity" not in _markers(history)
    assert "core_stream" in _markers(history)


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_python_store_gives_a_workflow_the_records_of_a_running_activity(
    client: Client, max_cached_workflows: int
):
    store = DictStreamStore()
    stream_client = _stream_client(client, store)
    chunks = await _filled_stream(stream_client, [], close=False)
    seen = await _filled_stream(stream_client, [], close=False)
    _, text = await _run(
        stream_client,
        LiveModelWorkflow,
        ModelCall(chunks.id, seen.id),
        activities=[stream_model_output],
        max_cached_workflows=max_cached_workflows,
    )
    assert text == "Hello, world"
    assert store.waiting == 0


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_python_store_read_with_wait_on_a_quiet_stream_gives_an_empty_page(
    client: Client, max_cached_workflows: int
):
    store = DictStreamStore()
    stream_client = _stream_client(client, store)
    ref = await _filled_stream(stream_client, [], close=False)
    handle, result = await _run(
        stream_client,
        ReadOnePageWorkflow,
        ref,
        1000,
        max_cached_workflows=max_cached_workflows,
    )
    assert result == [[], False, None]
    events = (await handle.fetch_history()).events
    elapsed = events[-1].event_time.ToDatetime() - events[0].event_time.ToDatetime()
    assert elapsed >= timedelta(seconds=1)
    assert store.waiting == 0


async def test_python_store_read_replays_from_the_store(client: Client):
    store = DictStreamStore()
    stream_client = _stream_client(client, store)
    ref = await _filled_stream(stream_client, ["a", "b", "c"])
    handle, result = await _run(
        stream_client, ReadPagesWorkflow, ReadArgs(ref.id, store="dict")
    )
    assert result.bodies == ["a", "b", "c"]
    # History holds no Record, so the replay reads them from the Python Store again.
    recording = RecordingStreamStore(store)
    await Replayer(
        workflows=[ReadPagesWorkflow],
        stream_config=StreamConfig(stores=[recording]),
    ).replay_workflow(await handle.fetch_history())
    assert "ReadRecords" in recording.operations


class ClosedOnAppendStore(DictStreamStore):
    async def append_records(
        self, request: AppendRecordsRequest
    ) -> AppendRecordsResponse:
        raise StreamError(
            "The Store closed the Stream",
            reason=StreamErrorReason.STREAM_ERROR_REASON_CLOSED,
        )


class BrokenReadStore(DictStreamStore):
    async def read_records(self, request: ReadRecordsRequest) -> ReadRecordsResponse:
        raise RuntimeError("the read broke")


@workflow.defn
class PythonStoreErrorWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str], call: str) -> list[Any]:
        stream = workflow.get_stream(ref, body_type=str)
        try:
            if call == "append":
                await stream.append("a")
            else:
                await stream.read_page()
        except StreamError as err:
            return [StreamErrorReason.Name(err.reason), err.message, err.non_retryable]
        return ["no error"]


async def test_python_store_error_reaches_workflow_code_with_its_reason(
    client: Client,
):
    stream_client = _stream_client(client, ClosedOnAppendStore())
    ref = await _filled_stream(stream_client, [], close=False)
    _, result = await _run(stream_client, PythonStoreErrorWorkflow, ref, "append")
    assert result == [
        "STREAM_ERROR_REASON_CLOSED",
        "The Store closed the Stream",
        True,
    ]


class UnavailableTwiceStore(DictStreamStore):
    """A Store whose first two appends fail with an outage."""

    def __init__(self) -> None:
        super().__init__()
        self.failed_appends = 0

    async def append_records(
        self, request: AppendRecordsRequest
    ) -> AppendRecordsResponse:
        if self.failed_appends < 2:
            self.failed_appends += 1
            raise StreamError(
                "The Store is down",
                reason=StreamErrorReason.STREAM_ERROR_REASON_STORAGE_UNAVAILABLE,
            )
        return await super().append_records(request)


async def test_python_store_outage_is_tried_again(client: Client):
    store = UnavailableTwiceStore()
    stream_client = _stream_client(client, store)
    ref = await _filled_stream(stream_client, [], close=False)
    _, offsets = await _run(stream_client, AppendWorkflow, ref)
    # Core tried the append again after each outage, and the third try wrote it.
    assert store.failed_appends == 2
    assert offsets == [(1).to_bytes(8, "big").hex(), (2).to_bytes(8, "big").hex()]
    assert [record.body for record in await _read_all(stream_client, ref)] == [
        "a",
        "b",
    ]


async def test_python_store_exception_is_a_stream_failure_and_the_worker_goes_on(
    client: Client,
):
    stream_client = _stream_client(client, BrokenReadStore())
    ref = await _filled_stream(stream_client, [], close=False)
    async with new_worker(
        stream_client, PythonStoreErrorWorkflow, PythonStoreAppendCloseWorkflow
    ) as worker:
        handle = await stream_client.start_workflow(
            PythonStoreErrorWorkflow.run,
            args=[ref, "read"],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        # A retry can't fix the Store, so Core gives the failure on the first try.
        result = await asyncio.wait_for(handle.result(), timeout=10)
        assert result == [
            "STREAM_ERROR_REASON_STORAGE_CORRUPT",
            "The Stream Store 'dict' raised RuntimeError: the read broke",
            True,
        ]
        # The same Worker serves the next Workflow.
        await stream_client.execute_workflow(
            PythonStoreAppendCloseWorkflow.run,
            ref,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert await _status(stream_client, ref) == StreamStatus.StreamStatusClosed


async def test_python_store_read_that_a_cancel_stops_leaves_no_task(client: Client):
    store = DictStreamStore()
    stream_client = _stream_client(client, store)
    ref = await _filled_stream(stream_client, ["a"], close=False)
    async with new_worker(stream_client, CancelDuringReadWorkflow) as worker:
        handle = await stream_client.start_workflow(
            CancelDuringReadWorkflow.run,
            ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        async def read_waits() -> None:
            assert store.waiting == 1

        await assert_eventually(read_waits)
        await handle.cancel()
        assert await asyncio.wait_for(handle.result(), timeout=30) == [
            ["a"],
            "cancelled",
        ]

        async def no_read_waits() -> None:
            assert store.waiting == 0

        await assert_eventually(no_read_waits)


# A payload codec.


@workflow.defn
class AppendThenReadWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> list[str]:
        stream = workflow.get_stream(ref, body_type=str)
        await stream.append("a", "b")
        return [record.body async for record in stream.read()]


async def test_payload_codec_covers_direct_append_and_read(client: Client):
    store = DictStreamStore()
    stream_client = _stream_client(client, store, codec=True)
    ref = await _filled_stream(stream_client, [], close=False)
    _, bodies = await _run(stream_client, AppendThenReadWorkflow, ref)
    assert bodies == ["a", "b"]
    # A client without the codec reads the bodies as the Store keeps them.
    raw = _stream_client(client, store).get_stream(
        StreamRef(id=ref.id), body_type=RawValue
    )
    stored = [record.body.payload async for record in raw.read()]
    assert [body.metadata["simple-codec"] for body in stored] == [b"true", b"true"]


# A close after a Direct append.


@workflow.defn
class AppendThenCloseWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> None:
        stream = workflow.get_stream(ref, body_type=str)
        await stream.append("a")
        await stream.close()


async def test_close_after_direct_append(client: Client):
    # The append writes as the Workflow with its task as the epoch. The close, from an
    # Activity, sends the same Workflow Producer with no epoch, and the Store fences it.
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    await _run(stream_client, AppendThenCloseWorkflow, ref)
    assert [record.body for record in await _read_all(stream_client, ref)] == ["a"]


# A measurement.


@dataclass
class Burst:
    stream_id: str
    records: int = 200
    batches: int = 20
    over_ms: int = 2000


@activity.defn
async def write_burst(burst: Burst) -> None:
    stream = activity.client().get_stream(StreamRef(id=burst.stream_id), body_type=str)
    size = burst.records // burst.batches
    for batch in range(burst.batches):
        await stream.append(
            *(f"record-{batch * size + index}" for index in range(size))
        )
        await asyncio.sleep(burst.over_ms / 1000 / burst.batches)
    await stream.close()


@workflow.defn
class MeasureWorkflow:
    @workflow.run
    async def run(self, burst: Burst) -> int:
        writer = workflow.start_activity(
            write_burst, burst, start_to_close_timeout=timedelta(seconds=60)
        )
        stream = workflow.get_stream(StreamRef(id=burst.stream_id), body_type=str)
        count = 0
        async for _ in stream.read(wait=timedelta(seconds=30)):
            count += 1
        await writer
        return count


@pytest.mark.parametrize(
    "replay_from", [StreamReplaySource.STORE, StreamReplaySource.HISTORY]
)
async def test_measure_read_of_200_records_over_2_seconds(
    client: Client, replay_from: StreamReplaySource
):
    stream_client = _stream_client(client, replay_from=replay_from)
    ref = await _filled_stream(stream_client, [], close=False)
    handle, count = await _run(
        stream_client, MeasureWorkflow, Burst(ref.id), activities=[write_burst]
    )
    assert count == 200
    history = await handle.fetch_history()
    sizes = [
        event.marker_recorded_event_attributes.ByteSize()
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    ]
    print(
        f"\n{replay_from.name}: {_completed_tasks(history)} Workflow Tasks, "
        f"{len(sizes)} markers, {sum(sizes)} marker bytes, sizes {sizes}"
    )


# Two loops from one offset, and calls on a read that fails.


@workflow.defn
class TwoLoopsWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> list[list[str]]:
        stream = workflow.get_stream(ref, body_type=str)

        async def drain() -> list[str]:
            return [record.body async for record in stream.read()]

        # Both loops ask for the first page before either gets it, so each one takes a
        # read of its own.
        first = asyncio.create_task(drain())
        second = asyncio.create_task(drain())
        return [await first, await second]


async def test_two_loops_from_one_offset_each_get_every_record(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b", "c"])
    handle, result = await _run(stream_client, TwoLoopsWorkflow, ref)
    assert result == [["a", "b", "c"], ["a", "b", "c"]]
    assert len(_read_seqs(await handle.fetch_history())) == 2


@workflow.defn
class FailedReadsWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> list[str]:
        stream = workflow.get_stream(ref, body_type=str)

        async def read() -> str:
            try:
                await stream.read_page(wait=timedelta(seconds=10))
            except StreamError as err:
                return StreamErrorReason.Name(err.reason)
            return "page"

        # Two calls wait before the failure comes, and a later call fails too.
        first = asyncio.create_task(read())
        second = asyncio.create_task(read())
        return [await first, await second, await read()]


async def test_each_read_of_a_missing_stream_raises(client: Client):
    stream_client = _stream_client(client)
    ref: StreamRef[str] = StreamRef(id=f"stream-{uuid.uuid4()}")
    _, result = await _run(stream_client, FailedReadsWorkflow, ref)
    assert result == ["STREAM_ERROR_REASON_NOT_FOUND"] * 3


# A codec that uses its serialization context.


@dataclass(frozen=True)
class ContextCodec(PayloadCodec, WithSerializationContext):
    """Writes the Workflow id of its context into each payload, and decodes only its own."""

    workflow_id: str = ""

    def with_context(self, context: SerializationContext) -> ContextCodec:
        if isinstance(context, WorkflowSerializationContext):
            return ContextCodec(context.workflow_id)
        return ContextCodec("")

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload(
                metadata={"encoding": b"binary/ctx", "ctx": self.workflow_id.encode()},
                data=payload.SerializeToString(),
            )
            for payload in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out = []
        for payload in payloads:
            if payload.metadata.get("encoding") != b"binary/ctx":
                out.append(payload)
                continue
            if payload.metadata["ctx"] != self.workflow_id.encode():
                raise ApplicationError(
                    f"Encoded for {payload.metadata['ctx']!r}, not "
                    f"{self.workflow_id!r}",
                    non_retryable=True,
                )
            out.append(Payload.FromString(payload.data))
        return out


@workflow.defn
class AppendThenReadAllWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> list[str]:
        stream = workflow.get_stream(ref, body_type=str)
        await stream.append("from-workflow")
        return [record.body async for record in stream.read()]


async def test_codec_runs_with_no_context_on_stream_bodies(client: Client):
    store = DictStreamStore()
    config = client.config()
    config["stream_config"] = StreamConfig(stores=[store])
    config["data_converter"] = DataConverter(payload_codec=ContextCodec())
    stream_client = Client(**config)
    ref = await _filled_stream(stream_client, ["from-client"], close=False)
    _, bodies = await _run(stream_client, AppendThenReadAllWorkflow, ref)
    assert bodies == ["from-client", "from-workflow"]
    # The client decodes with no context, so it reads the Workflow's body too.
    assert [record.body for record in await _read_all(stream_client, ref)] == [
        "from-client",
        "from-workflow",
    ]
    raw_config = client.config()
    raw_config["stream_config"] = StreamConfig(stores=[store])
    raw = Client(**raw_config).get_stream(StreamRef(id=ref.id), body_type=RawValue)
    stored = [record.body.payload async for record in raw.read()]
    assert [body.metadata["ctx"] for body in stored] == [b"", b""]


@workflow.defn
class AppendOneWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> None:
        await workflow.get_stream(ref, body_type=str).append("from-workflow")


# Replays with no Stream config of calls whose ref names no Store.


@workflow.defn
class UnnamedRefReadWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        return [record.body async for record in stream.read()]


async def _unnamed_read_history(
    client: Client, replay_from: StreamReplaySource
) -> tuple[WorkflowHistory, str]:
    stream_client = _stream_client(client, replay_from=replay_from)
    ref = await _filled_stream(stream_client, ["a", "b"])
    handle, result = await _run(stream_client, UnnamedRefReadWorkflow, ref.id)
    assert result == ["a", "b"]
    return await handle.fetch_history(), ref.id


async def test_history_replay_of_an_unnamed_ref_needs_no_config(client: Client):
    history, _ = await _unnamed_read_history(client, StreamReplaySource.HISTORY)
    await Replayer(workflows=[UnnamedRefReadWorkflow]).replay_workflow(history)


async def test_store_replay_of_an_unnamed_ref_with_no_config_fails(client: Client):
    history, stream_id = await _unnamed_read_history(client, StreamReplaySource.STORE)
    with pytest.raises(RuntimeError) as err:
        await Replayer(workflows=[UnnamedRefReadWorkflow]).replay_workflow(history)
    message = str(err.value)
    assert f"StreamReplayError: Stream `{stream_id}` in Store `dict`" in message
    assert "this Worker has no Stream runtime for the Store `dict`" in message
    assert "Nondeterminism" not in message


@workflow.defn
class UnnamedRefAppendWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str | None:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        appended = await stream.append("a")
        return _hex(appended.last_offset)


async def test_replay_of_an_unnamed_append_needs_no_config(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    handle, _ = await _run(stream_client, UnnamedRefAppendWorkflow, ref.id)
    history = await handle.fetch_history()
    assert _markers(history) == ["core_stream"]
    await Replayer(workflows=[UnnamedRefAppendWorkflow]).replay_workflow(history)


# A Record whose body fails to decode.


@dataclass
class Point:
    x: int


@workflow.defn
class BadBodyWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[Any]:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=Point)
        first = await stream.read_page(max_records=1)
        events: list[Any] = [["got", first.records[0].body.x]]
        # The page of the bad body raises, and a read from the same offset gets it again.
        for _ in range(2):
            try:
                await stream.read_page(from_offset=first.last_offset, max_records=1)
                events.append(["page", "decoded"])
            except Exception as err:
                events.append(["page", type(err).__name__])
        return events


async def test_page_with_a_body_that_fails_to_decode_raises_each_time(
    client: Client,
):
    stream_client = _stream_client(client)
    stream = await stream_client.create_stream(id=f"s-{uuid.uuid4()}", body_type=object)
    await stream.append({"x": 1}, "not a point", {"x": 3})
    await stream.close()
    _, events = await _run(stream_client, BadBodyWorkflow, stream.ref.id)
    assert events == [["got", 1], ["page", "TypeError"], ["page", "TypeError"]]


# The Direct close.


def _close_entries(history: WorkflowHistory) -> list[StreamCloseEntry]:
    """Give the close entries of the `core_stream` markers, in order."""
    entries: list[StreamCloseEntry] = []
    for event in history.events:
        attributes = event.marker_recorded_event_attributes
        if (
            event.event_type != EventType.EVENT_TYPE_MARKER_RECORDED
            or attributes.marker_name != "core_stream"
        ):
            continue
        [payload] = attributes.details["data"].payloads
        entries.extend(StreamMarkerData.FromString(payload.data).closes)
    return entries


def _scheduled_activities(history: WorkflowHistory) -> list[str]:
    return [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]


async def _status(client: Client, ref: StreamRef[str]) -> StreamStatus | None:
    return (await client.get_stream(ref, body_type=str).describe()).status


@workflow.defn
class AppendMaybeCloseWorkflow:
    @workflow.run
    async def run(self, stream_id: str, close: bool) -> None:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        await stream.append("a")
        if close:
            await stream.close()


async def test_awaited_close_closes_in_one_more_task(client: Client):
    stream_client = _stream_client(client)
    closed_ref = await _filled_stream(stream_client, [], close=False)
    open_ref = await _filled_stream(stream_client, [], close=False)
    closed, _ = await _run(stream_client, AppendMaybeCloseWorkflow, closed_ref.id, True)
    left_open, _ = await _run(
        stream_client, AppendMaybeCloseWorkflow, open_ref.id, False
    )
    assert await _status(stream_client, closed_ref) == StreamStatus.StreamStatusClosed
    assert await _status(stream_client, open_ref) == StreamStatus.StreamStatusOpen
    history = await closed.fetch_history()
    assert _scheduled_activities(history) == []
    assert "core_local_activity" not in _markers(history)
    # The task of the close records its intent, and the next one gives the result.
    assert _completed_tasks(history) == (
        _completed_tasks(await left_open.fetch_history()) + 1
    )
    intent, result = _close_entries(history)
    assert intent.HasField("intent_epoch") and not intent.resolved
    assert result.resolved and not result.HasField("failure")


@workflow.defn
class FinallyCloseWorkflow:
    @workflow.run
    async def run(self, stream_id: str, fail: bool) -> str:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        try:
            await stream.append("a")
            if fail:
                raise ApplicationError("failed on purpose", non_retryable=True)
            return "returned"
        finally:
            await stream.close()


@pytest.mark.parametrize("fail", [False, True])
async def test_close_in_finally(client: Client, fail: bool):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    if fail:
        with pytest.raises(WorkflowFailureError) as err:
            await _run(stream_client, FinallyCloseWorkflow, ref.id, True)
        assert isinstance(err.value.cause, ApplicationError)
        assert err.value.cause.message == "failed on purpose"
    else:
        _, result = await _run(stream_client, FinallyCloseWorkflow, ref.id, False)
        assert result == "returned"
    assert await _status(stream_client, ref) == StreamStatus.StreamStatusClosed
    assert [record.body for record in await _read_all(stream_client, ref)] == ["a"]


@workflow.defn
class AppendAfterCloseWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        await stream.append("a")
        await stream.close()
        try:
            await stream.append("b")
        except StreamError as err:
            return [StreamErrorReason.Name(err.reason), str(err.non_retryable)]
        return ["appended"]


async def test_append_after_the_close_is_refused(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    _, result = await _run(stream_client, AppendAfterCloseWorkflow, ref.id)
    assert result == ["STREAM_ERROR_REASON_CLOSED", "True"]
    assert [record.body for record in await _read_all(stream_client, ref)] == ["a"]


_fail_close_task = True


@workflow.defn(sandboxed=False)
class FailAfterCloseOnceWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        await stream.append("a")
        close = asyncio.create_task(stream.close())
        # A few turns of the loop let the close send its command in this activation.
        for _ in range(10):
            await asyncio.sleep(0)
        global _fail_close_task
        if _fail_close_task:
            _fail_close_task = False
            raise RuntimeError("Failed the Workflow Task once")
        await close


async def test_failed_task_after_the_close_leaves_the_stream_open(client: Client):
    global _fail_close_task
    _fail_close_task = True
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    handle, _ = await _run(stream_client, FailAfterCloseOnceWorkflow, ref.id)
    assert not _fail_close_task
    # The retry could append, so the failed task closed nothing.
    assert [record.body for record in await _read_all(stream_client, ref)] == ["a", "a"]
    assert await _status(stream_client, ref) == StreamStatus.StreamStatusClosed
    # History has one intent and one result, from the retry.
    entries = _close_entries(await handle.fetch_history())
    assert [entry.resolved for entry in entries] == [False, True]


@workflow.defn
class CloseTwiceWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        await stream.close()
        await stream.close()
        return "closed"


async def test_close_of_a_closed_stream_succeeds(client: Client):
    stream_client = _stream_client(client)
    # A client closed this Stream before the Workflow closes it twice.
    ref = await _filled_stream(stream_client, ["a"])
    _, result = await _run(stream_client, CloseTwiceWorkflow, ref.id)
    assert result == "closed"
    open_ref = await _filled_stream(stream_client, ["a"], close=False)
    _, result = await _run(stream_client, CloseTwiceWorkflow, open_ref.id)
    assert result == "closed"
    assert await _status(stream_client, open_ref) == StreamStatus.StreamStatusClosed


async def test_close_without_a_cache_and_in_replays(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, [], close=False)
    handle, _ = await _run(
        stream_client, AppendMaybeCloseWorkflow, ref.id, True, max_cached_workflows=0
    )
    assert [record.body for record in await _read_all(stream_client, ref)] == ["a"]
    assert await _status(stream_client, ref) == StreamStatus.StreamStatusClosed
    history = await handle.fetch_history()
    assert [entry.resolved for entry in _close_entries(history)] == [False, True]
    # A Store with the same Stream, still open, shows that a replay closes nothing.
    replay_store = DictStreamStore()
    replay_client = _stream_client(client, replay_store)
    await replay_client.create_stream(id=ref.id, body_type=str)
    await Replayer(
        workflows=[AppendMaybeCloseWorkflow],
        stream_config=replay_client.config()["stream_config"],
    ).replay_workflow(history)
    assert await _status(replay_client, ref) == StreamStatus.StreamStatusOpen
    # The ref names no Store, so a Replayer with no Stream config takes it from History.
    await Replayer(workflows=[AppendMaybeCloseWorkflow]).replay_workflow(history)


@workflow.defn
class CloseNotAwaitedWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        # The task sends the close command, and the Workflow returns at once.
        asyncio.create_task(stream.close())
        await asyncio.sleep(0)
        return "returned"


async def test_close_not_awaited_closes_after_the_workflow_ends(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a"], close=False)
    handle, result = await _run(stream_client, CloseNotAwaitedWorkflow, ref.id)
    assert result == "returned"

    async def closed() -> bool:
        return await _status(stream_client, ref) == StreamStatus.StreamStatusClosed

    for _ in range(50):
        if await closed():
            break
        await asyncio.sleep(0.1)
    assert await closed()
    # The task that ends the Workflow has the intent, and no task gives a result.
    entries = _close_entries(await handle.fetch_history())
    assert [entry.resolved for entry in entries] == [False]


async def test_shutdown_waits_for_a_close_not_awaited(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a"], close=False)
    async with new_worker(stream_client, CloseNotAwaitedWorkflow) as worker:
        result = await stream_client.execute_workflow(
            CloseNotAwaitedWorkflow.run,
            ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert result == "returned"
    # A graceful shutdown returns only after the Store answered the close in flight, so
    # the Stream is closed with no wait here.
    assert await _status(stream_client, ref) == StreamStatus.StreamStatusClosed


@activity.defn
async def sleep_a_while() -> None:
    await asyncio.sleep(2)


@workflow.defn
class CancelDuringCloseWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        # The Local Activity holds the task of the close open, so the cancel comes in the
        # task that gives the result of the close.
        workflow.start_local_activity(
            sleep_a_while, start_to_close_timeout=timedelta(seconds=10)
        )
        try:
            await stream.close()
        except asyncio.CancelledError:
            return "cancelled"
        return "closed"


async def test_cancel_while_the_close_waits(client: Client):
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a"], close=False)
    async with new_worker(
        stream_client, CancelDuringCloseWorkflow, activities=[sleep_a_while]
    ) as worker:
        handle = await stream_client.start_workflow(
            CancelDuringCloseWorkflow.run,
            ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await asyncio.sleep(0.5)
        await handle.cancel()
        result = await asyncio.wait_for(handle.result(), timeout=30)
    # The code gets the cancel, and Core still closed the Stream after the task of the
    # intent.
    assert result == "cancelled"
    assert await _status(stream_client, ref) == StreamStatus.StreamStatusClosed


# The metrics of the Direct calls.


@workflow.defn
class ReadAppendCloseWorkflow:
    @workflow.run
    async def run(self, source_id: str, out_id: str) -> list[str]:
        source = workflow.get_stream(StreamRef(id=source_id), body_type=str)
        out = workflow.get_stream(StreamRef(id=out_id), body_type=str)
        got = [record.body async for record in source.read()]
        await out.append(*got)
        await out.close()
        return got


async def test_direct_calls_record_their_metrics(env: WorkflowEnvironment):
    buffer = MetricBuffer(10000)
    runtime = Runtime(telemetry=TelemetryConfig(metrics=buffer))
    client = await env.connect_client(runtime=runtime)
    store = DictStreamStore(name="metered")
    stream_client = _stream_client(client, store)
    source = await _filled_stream(stream_client, ["a", "b", "c"])
    out = await _filled_stream(stream_client, [], close=False)
    async with new_worker(stream_client, ReadAppendCloseWorkflow) as worker:
        result = await stream_client.execute_workflow(
            ReadAppendCloseWorkflow.run,
            args=[source.id, out.id],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert result == ["a", "b", "c"]
    updates = [
        update
        for update in buffer.retrieve_updates()
        if update.metric.name.startswith("temporal_stream_")
    ]
    print("\nStream metrics:", sorted({update.metric.name for update in updates}))

    def values(name: str) -> list[Any]:
        return [update.value for update in updates if update.metric.name == name]

    assert sum(values("temporal_stream_read_total")) == 1
    assert sum(values("temporal_stream_read_records")) == 3
    assert sum(values("temporal_stream_append_total")) == 1
    assert sum(values("temporal_stream_close_total")) == 1
    assert len(values("temporal_stream_append_latency")) == 1
    assert len(values("temporal_stream_close_latency")) == 1
    assert values("temporal_stream_read_failed") == []
    assert values("temporal_stream_append_failed") == []
    assert values("temporal_stream_close_failed") == []
    for update in updates:
        # The Stream runtime counts the reads that wait on the Store, with the default
        # attributes of the meter and no more.
        if update.metric.name == "temporal_stream_blocking_reads_waiting":
            continue
        assert update.attributes["stream_store"] == "metered"
        assert update.attributes["workflow_type"] == "ReadAppendCloseWorkflow"
        assert update.attributes["task_queue"] == worker.task_queue


# The example of the README: a Workflow starts each tool as its call arrives.


@dataclass
class ToolCall:
    name: str
    argument: str


@dataclass
class ModelEvent:
    kind: str
    text: str | None = None
    tool_call: ToolCall | None = None


_MODEL_EVENTS = [
    ModelEvent(kind="text", text="Let me look."),
    ModelEvent(kind="tool_call", tool_call=ToolCall(name="weather", argument="Oslo")),
    ModelEvent(kind="text", text="And the time."),
    ModelEvent(kind="tool_call", tool_call=ToolCall(name="clock", argument="Oslo")),
    ModelEvent(kind="text", text="Done."),
]


@activity.defn
async def model_answer(events_id: str) -> None:
    events = activity.client().get_stream(StreamRef(id=events_id), body_type=ModelEvent)
    for event in _MODEL_EVENTS:
        await events.append(event)
        # The model takes a while between its events, so a tool can start before it ends.
        await asyncio.sleep(0.5)
    await events.close()


@activity.defn
async def run_tool(call: ToolCall) -> str:
    return f"{call.name}({call.argument})"


@workflow.defn
class ToolCallsWorkflow:
    @workflow.run
    async def run(self, events_id: str, status_id: str) -> list[str]:
        model = workflow.start_activity(
            model_answer, events_id, start_to_close_timeout=timedelta(seconds=30)
        )
        events = workflow.get_stream(StreamRef(id=events_id), body_type=ModelEvent)
        status = workflow.get_stream(StreamRef(id=status_id), body_type=str)
        tools = []
        async for record in events.read(wait=timedelta(seconds=10)):
            event = record.body
            if event.kind == "tool_call":
                assert event.tool_call is not None
                # The tool starts now, before the model ends its answer.
                tools.append(
                    workflow.start_activity(
                        run_tool,
                        event.tool_call,
                        start_to_close_timeout=timedelta(minutes=1),
                    )
                )
                await status.append(f"Running {event.tool_call.name}")
        results = await asyncio.gather(*tools)
        await model
        return list(results)


async def test_workflow_starts_each_tool_as_its_call_arrives(client: Client):
    stream_client = _stream_client(client)
    events = await _filled_stream(stream_client, [], close=False)
    status = await _filled_stream(stream_client, [], close=False)
    handle, results = await _run(
        stream_client,
        ToolCallsWorkflow,
        events.id,
        status.id,
        activities=[model_answer, run_tool],
    )
    assert results == ["weather(Oslo)", "clock(Oslo)"]
    assert [record.body for record in await _read_all(stream_client, status)] == [
        "Running weather",
        "Running clock",
    ]
    # The first tool was scheduled while the model Activity still ran.
    history = await handle.fetch_history()
    scheduled: dict[int, str] = {}
    first_tool = None
    model_completed = None
    for event in history.events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            name = event.activity_task_scheduled_event_attributes.activity_type.name
            scheduled[event.event_id] = name
            if name == "run_tool" and first_tool is None:
                first_tool = event.event_id
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            attributes = event.activity_task_completed_event_attributes
            if scheduled[attributes.scheduled_event_id] == "model_answer":
                model_completed = event.event_id
    assert first_tool is not None and model_completed is not None
    assert first_tool < model_completed


# The read-ahead bound.


def test_read_ahead_bytes_below_one_raises():
    for bytes_ in (0, -1):
        with pytest.raises(ValueError, match="workflow_read_ahead_bytes"):
            StreamConfig(stores=[DictStreamStore()], workflow_read_ahead_bytes=bytes_)


async def test_read_with_a_read_ahead_of_one_byte_gives_every_record(client: Client):
    config = client.config()
    config["stream_config"] = StreamConfig(
        stores=[DictStreamStore()], workflow_read_ahead_bytes=1
    )
    stream_client = Client(**config)
    bodies = [f"record-{index}" for index in range(20)]
    ref = await _filled_stream(stream_client, bodies)
    _, result = await _run(stream_client, ReadPagesWorkflow, ReadArgs(ref.id))
    assert result.bodies == bodies
    assert result.dones[-1] is True


# The cap on the Records of one page that Workflow code gets.


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_store_page_over_256_records_comes_in_pages_of_at_most_256(
    client: Client, max_cached_workflows: int
):
    stream_client = _stream_client(client)
    bodies = [f"record-{index}" for index in range(600)]
    ref = await _filled_stream(stream_client, bodies)
    handle, got = await _run(
        stream_client,
        ReadItemsWorkflow,
        ref,
        None,
        max_cached_workflows=max_cached_workflows,
    )
    assert got == bodies
    history = await handle.fetch_history()
    counts = [
        count
        for task in _read_entries(history)
        for entry in task
        for count in entry.page_counts
    ]
    assert sum(counts) == 600
    assert max(counts) == 256
    config = stream_client.config()["stream_config"]
    await Replayer(workflows=[ReadItemsWorkflow], stream_config=config).replay_workflow(
        history
    )


# The page cap and the least gap of a read, two settings of the Worker.


def _client_with(client: Client, **settings: Any) -> Client:
    config = client.config()
    config["stream_config"] = StreamConfig(stores=[DictStreamStore()], **settings)
    return Client(**config)


def _page_counts(history: WorkflowHistory) -> list[int]:
    return [
        count
        for task in _read_entries(history)
        for entry in task
        for count in entry.page_counts
    ]


@pytest.mark.parametrize("max_cached_workflows", [1000, 0])
async def test_page_cap_of_10_gives_pages_of_at_most_10(
    client: Client, max_cached_workflows: int
):
    stream_client = _client_with(client, workflow_read_page_records=10)
    bodies = [f"record-{index}" for index in range(35)]
    ref = await _filled_stream(stream_client, bodies)
    handle, got = await _run(
        stream_client,
        ReadItemsWorkflow,
        ref,
        None,
        max_cached_workflows=max_cached_workflows,
    )
    assert got == bodies
    history = await handle.fetch_history()
    counts = _page_counts(history)
    assert sum(counts) == 35
    assert max(counts) == 10
    config = stream_client.config()["stream_config"]
    await Replayer(workflows=[ReadItemsWorkflow], stream_config=config).replay_workflow(
        history
    )


def test_page_cap_out_of_bounds_raises():
    for records in (0, 1001):
        with pytest.raises(ValueError, match="workflow_read_page_records"):
            StreamConfig(stores=[DictStreamStore()], workflow_read_page_records=records)


def test_min_gap_out_of_bounds_raises():
    for gap in (timedelta(seconds=2), timedelta(milliseconds=-1)):
        with pytest.raises(ValueError, match="workflow_read_min_gap"):
            StreamConfig(stores=[DictStreamStore()], workflow_read_min_gap=gap)


@activity.defn
async def append_five_20ms_apart(stream_id: str) -> None:
    stream = activity.client().get_stream(StreamRef(id=stream_id), body_type=str)
    for index in range(5):
        await stream.append(f"record-{index}")
        await asyncio.sleep(0.02)
    await stream.close()


@workflow.defn
class GappedReadWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> ReadResult:
        writer = workflow.start_activity(
            append_five_20ms_apart,
            stream_id,
            start_to_close_timeout=timedelta(seconds=30),
        )
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        result = ReadResult([], [], [], [], [])
        async for page in stream.read_pages(wait=timedelta(seconds=10)):
            result.bodies.extend(record.body for record in page.records)
            result.page_sizes.append(len(page.records))
            result.dones.append(page.done)
        await writer
        return result


async def test_min_gap_of_200ms_joins_records_that_come_20ms_apart(client: Client):
    stream_client = _client_with(
        client, workflow_read_min_gap=timedelta(milliseconds=200)
    )
    ref = await _filled_stream(stream_client, [], close=False)
    handle, result = await _run(
        stream_client,
        GappedReadWorkflow,
        ref.id,
        activities=[append_five_20ms_apart],
    )
    assert result.bodies == [f"record-{index}" for index in range(5)]
    assert len(result.page_sizes) < 5
    assert result.dones[-1] is True
    config = stream_client.config()["stream_config"]
    await Replayer(
        workflows=[GappedReadWorkflow], stream_config=config
    ).replay_workflow(await handle.fetch_history())


# A replay that lost the Records of a read.


class _ReadThenWait:
    def __init__(self) -> None:
        self.go = False

    async def read_then_wait(self, stream_id: str) -> list[str]:
        stream = workflow.get_stream(StreamRef(id=stream_id), body_type=str)
        got = [record.body async for record in stream.read()]
        await workflow.wait_condition(lambda: self.go)
        return got

    @workflow.signal
    def proceed(self) -> None:
        self.go = True


@workflow.defn
class ReadThenWaitWorkflow(_ReadThenWait):
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        return await self.read_then_wait(stream_id)


@workflow.defn(failure_exception_types=[StreamReplayError])
class ReadThenWaitFailsWorkflow(_ReadThenWait):
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        return await self.read_then_wait(stream_id)


async def _replay_with_records_gone(
    client: Client, workflow_type: type[Any], **worker_options: Any
) -> WorkflowHandle[Any, Any]:
    """Read on one Worker, then give the run to a Worker whose Store lost the Records."""
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b"])
    task_queue = f"tq-{uuid.uuid4()}"
    # No cache, so the next Worker replays the History from the start.
    async with new_worker(
        stream_client, workflow_type, task_queue=task_queue, max_cached_workflows=0
    ):
        handle: WorkflowHandle[Any, Any] = await stream_client.start_workflow(
            workflow_type.run,
            ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=task_queue,
        )

        async def read_is_in_history() -> None:
            assert "core_stream" in _markers(await handle.fetch_history())

        await assert_eventually(read_is_in_history)
    # A new Store of the same name has none of the Records.
    lost_client = _stream_client(client)
    worker = new_worker(
        lost_client, workflow_type, task_queue=task_queue, **worker_options
    )
    async with worker:
        await handle.signal("proceed")
        if workflow_type is ReadThenWaitWorkflow and not worker_options:
            await assert_task_fail_eventually(
                handle, message_contains=f"StreamReplayError: Stream `{ref.id}`"
            )
            await handle.terminate()
            return handle
        with pytest.raises(WorkflowFailureError) as err:
            await asyncio.wait_for(handle.result(), timeout=30)
    cause = err.value.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == "StreamReplayError"
    assert f"StreamReplayError: Stream `{ref.id}`" in cause.message
    return handle


async def test_lost_records_fail_the_task_by_default(client: Client):
    await _replay_with_records_gone(client, ReadThenWaitWorkflow)


async def test_lost_records_fail_the_workflow_with_the_worker_option(client: Client):
    await _replay_with_records_gone(
        client,
        ReadThenWaitWorkflow,
        workflow_failure_exception_types=[StreamReplayError],
    )


async def test_lost_records_fail_the_workflow_with_the_defn_option(client: Client):
    await _replay_with_records_gone(client, ReadThenWaitFailsWorkflow)


# A Workflow outbound interceptor sees each page that Workflow code reads.

_intercepted: list[tuple[str, Any]] = []


class StreamCallInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor]:
        return _StreamCallInbound


class _StreamCallInbound(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_StreamCallOutbound(outbound))


class _StreamCallOutbound(WorkflowOutboundInterceptor):
    async def start_read_records(
        self,
        request: stream_service.ReadRecordsRequest[Any],
        output_type: type[stream_service.ReadRecordsResponse[Any]] | None = None,
    ) -> workflow.NexusOperationHandle[stream_service.ReadRecordsResponse[Any]]:
        _intercepted.append(("ReadRecords", request))
        return await super().start_read_records(request, output_type)


async def test_workflow_interceptor_sees_each_page_read(client: Client):
    _intercepted.clear()
    stream_client = _stream_client(client)
    ref = await _filled_stream(stream_client, ["a", "b", "c", "d", "e"])
    async with new_worker(
        stream_client, ReadPagesWorkflow, interceptors=[StreamCallInterceptor()]
    ) as worker:
        result = await stream_client.execute_workflow(
            ReadPagesWorkflow.run,
            ReadArgs(ref.id, max_records=2),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert result.page_sizes == [2, 2, 1]
    assert [name for name, _ in _intercepted] == ["ReadRecords"] * 3
    # Each page starts after the last Record of the page before it.
    assert [request.from_offset for _, request in _intercepted] == [
        None,
        bytes.fromhex(result.offsets[1]),
        bytes.fromhex(result.offsets[3]),
    ]
