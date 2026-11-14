"""Tests of a Workflow read of a Stream that waits for a predicate."""

from __future__ import annotations

import asyncio
import collections
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest

from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.api.stream.v1 import StreamRef as StreamRefMessage
from temporalio.api.streamservice.v1 import ReadRecordsRequest, ReadRecordsResponse
from temporalio.client import (
    Client,
    StreamConfig,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowHistory,
)
from temporalio.client._stream_read import _PENDING_STREAM_READS
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.nexus.system.stream_service import StreamRecord, StreamRef
from temporalio.nexus.system.stream_service.workflow._resources.stream import (
    Stream as WorkflowStream,
)
from temporalio.testing import MemoryStreamStore
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

# The Workflows and the test share the values of `StreamReadState`, so the sandbox
# must not load its own copy of the module.
with workflow.unsafe.imports_passed_through():
    from tests.helpers import assert_eventually, assert_task_fail_eventually, new_worker
    from tests.helpers.streams import RecordingStreamStore, StreamReadState
    from tests.test_converter import SimpleCodec

# Every body carries this text, so a search of History shows whether a body leaks.
_BODY_MARK = "secret-body"


@dataclass
class Token:
    kind: str
    text: str


@dataclass
class ReadArgs:
    stream_id: str
    until_kind: str | None = "done"
    timeout_seconds: float = 10.0
    max_records: int | None = None
    idle_timeout_seconds: float | None = None
    max_bytes: int | None = None
    from_offset: str | None = None


@dataclass
class ReadResult:
    texts: list[str]
    stop: str
    done: bool
    last_offset: str | None
    typed: bool


def _result(read: workflow.StreamRead[Token]) -> ReadResult:
    return ReadResult(
        texts=[record.body.text for record in read.records],
        stop=read.stop.value,
        done=read.done,
        last_offset=read.last_offset.hex() if read.last_offset else None,
        typed=all(
            isinstance(record, StreamRecord) and isinstance(record.body, Token)
            for record in read.records
        ),
    )


def _keep(result: ReadResult) -> ReadResult:
    # A replay gives no result to the test, so the Workflow keeps it here.
    StreamReadState.results[workflow.info().run_id] = (
        tuple(result.texts),
        result.stop,
        result.done,
        result.last_offset,
    )
    return result


def _stream(stream_id: str) -> WorkflowStream[Token]:
    return workflow.get_stream(StreamRef(id=stream_id), body_type=Token)


@workflow.defn
class ReadUntilKindWorkflow:
    @workflow.run
    async def run(self, args: ReadArgs) -> ReadResult:
        until_kind = args.until_kind

        def until(record: StreamRecord[Token]) -> bool:
            StreamReadState.until_calls += 1
            assert isinstance(record.body, Token)
            return record.body.kind == until_kind

        read = await workflow.read_stream(
            _stream(args.stream_id),
            until=until if until_kind is not None else None,
            from_offset=bytes.fromhex(args.from_offset) if args.from_offset else None,
            idle_timeout=None
            if args.idle_timeout_seconds is None
            else timedelta(seconds=args.idle_timeout_seconds),
            timeout=timedelta(seconds=args.timeout_seconds),
            max_records=args.max_records,
            max_bytes=args.max_bytes,
        )
        return _keep(_result(read))


@workflow.defn
class ReadUntilWantedWorkflow:
    def __init__(self) -> None:
        self.wanted: str | None = None

    @workflow.signal
    def want(self, text: str) -> None:
        self.wanted = text

    @workflow.run
    async def run(self, stream_id: str) -> ReadResult:
        read = await workflow.read_stream(
            _stream(stream_id),
            until=lambda record: record.body.text == self.wanted,
            timeout=timedelta(seconds=20),
        )
        return _result(read)


@workflow.defn
class TwoReadsWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        stream = _stream(stream_id)
        first = await workflow.read_stream(
            stream,
            until=lambda record: record.body.text.endswith("m1"),
            timeout=timedelta(seconds=10),
        )
        await workflow.sleep(timedelta(milliseconds=1))
        second = await workflow.read_stream(
            stream,
            until=lambda record: record.body.text.endswith("m2"),
            from_offset=first.last_offset,
            timeout=timedelta(seconds=10),
        )
        return [record.body.text for record in [*first.records, *second.records]]


@workflow.defn
class FailTaskOnceAfterReadWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> ReadResult:
        read = await workflow.read_stream(
            _stream(stream_id),
            until=lambda record: record.body.kind == "done",
            timeout=timedelta(seconds=10),
        )
        # The failure comes in a later Workflow Task, so the retry replays the read.
        await workflow.sleep(timedelta(milliseconds=1))
        if StreamReadState.fail_next_task:
            StreamReadState.fail_next_task = False
            raise RuntimeError("Failed the Workflow Task once")
        return _result(read)


@workflow.defn
class TimeoutThenReadWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[ReadResult]:
        stream = _stream(stream_id)
        first = await workflow.read_stream(
            stream,
            until=lambda record: record.body.kind == "done",
            timeout=timedelta(seconds=1),
        )
        second = await workflow.read_stream(
            stream,
            until=lambda record: record.body.kind == "done",
            from_offset=first.last_offset,
            timeout=timedelta(seconds=10),
        )
        return [_result(first), _result(second)]


def _raise_on_record(record: StreamRecord[Token]) -> bool:
    raise ValueError(f"bad token {record.body.text}")


def _call_workflow_now(record: StreamRecord[Token]) -> bool:
    del record
    return workflow.now() > datetime.min


def _call_datetime_now(record: StreamRecord[Token]) -> bool:
    del record
    return datetime.now() > datetime.min


_PREDICATES: dict[str, Callable[[StreamRecord[Token]], bool]] = {
    "raise": _raise_on_record,
    "workflow_now": _call_workflow_now,
    "datetime_now": _call_datetime_now,
}


@workflow.defn
class FailingPredicateWorkflow:
    @workflow.run
    async def run(self, stream_id: str, predicate: str) -> str:
        try:
            await workflow.read_stream(
                _stream(stream_id),
                until=_PREDICATES[predicate],
                timeout=timedelta(seconds=10),
            )
        except ApplicationError as err:
            return f"{err.type}: {err.message}"
        return "no error"


@workflow.defn
class MaxBytesThenNextWorkflow:
    @workflow.run
    async def run(self, stream_id: str, max_bytes: int) -> list[ReadResult]:
        stream = _stream(stream_id)
        first = await workflow.read_stream(
            stream,
            idle_timeout=timedelta(0),
            timeout=timedelta(seconds=10),
            max_bytes=max_bytes,
        )
        second = await workflow.read_stream(
            stream,
            from_offset=first.last_offset,
            timeout=timedelta(seconds=10),
            max_records=1,
        )
        return [_result(first), _result(second)]


@dataclass
class ReduceArgs:
    stream_id: str
    until: str = "done"
    timeout_seconds: float = 10.0


@dataclass
class ReduceResult:
    value: str
    count: int
    stop: str
    done: bool
    last_offset: str | None


def _join(value: str, record: StreamRecord[Token]) -> str:
    StreamReadState.function_calls += 1
    return value + record.body.text


def _until_done(value: str, record: StreamRecord[Token]) -> bool:
    del value
    StreamReadState.until_calls += 1
    return record.body.kind == "done"


def _until_tag(value: str, record: StreamRecord[Token]) -> bool:
    del record
    StreamReadState.until_calls += 1
    return value.endswith("tag!")


_FOLD_UNTIL: dict[str, Callable[[str, StreamRecord[Token]], bool]] = {
    "done": _until_done,
    "tag": _until_tag,
}


def _reduce_result(reduce: workflow.StreamReduce[str]) -> ReduceResult:
    return ReduceResult(
        value=reduce.value,
        count=reduce.count,
        stop=reduce.stop.value,
        done=reduce.done,
        last_offset=reduce.last_offset.hex() if reduce.last_offset else None,
    )


@workflow.defn
class JoinTextWorkflow:
    @workflow.run
    async def run(self, args: ReduceArgs) -> ReduceResult:
        reduce = await workflow.reduce_stream(
            _stream(args.stream_id),
            _join,
            "",
            until=_FOLD_UNTIL[args.until],
            timeout=timedelta(seconds=args.timeout_seconds),
        )
        result = _reduce_result(reduce)
        StreamReadState.results[workflow.info().run_id] = (
            result.value,
            result.count,
            result.stop,
            result.done,
            result.last_offset,
        )
        return result


@workflow.defn
class TwoFoldsWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[ReduceResult]:
        stream = _stream(stream_id)
        first = await workflow.reduce_stream(
            stream,
            _join,
            "",
            until=lambda value, _: value.endswith("m1"),
            timeout=timedelta(seconds=10),
        )
        await workflow.sleep(timedelta(milliseconds=1))
        second = await workflow.reduce_stream(
            stream,
            _join,
            "",
            until=lambda value, _: value.endswith("m2"),
            from_offset=first.last_offset,
            timeout=timedelta(seconds=10),
        )
        return [_reduce_result(first), _reduce_result(second)]


def _raise_in_function(value: str, record: StreamRecord[Token]) -> str:
    raise ValueError(f"bad fold after {value!r} at {record.body.text}")


@workflow.defn
class FailingFunctionWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        try:
            await workflow.reduce_stream(
                _stream(stream_id),
                _raise_in_function,
                "",
                timeout=timedelta(seconds=10),
            )
        except ApplicationError as err:
            return f"{err.type}: {err.message}"
        return "no error"


@dataclass
class Summary:
    count: int
    kinds: list[str]


def _summarize(value: Summary, record: StreamRecord[Token]) -> Summary:
    return Summary(value.count + 1, [*value.kinds, record.body.kind])


def _collect(value: list[str], record: StreamRecord[Token]) -> list[str]:
    return [*value, record.body.text]


@dataclass
class TypedFoldResult:
    summary: Summary
    summary_type: str
    texts: list[str]
    texts_type: str


@workflow.defn
class TypedFoldWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> TypedFoldResult:
        stream = _stream(stream_id)
        summary = await workflow.reduce_stream(
            stream,
            _summarize,
            Summary(0, []),
            idle_timeout=timedelta(0),
            timeout=timedelta(seconds=10),
        )
        no_texts: list[str] = []
        texts = await workflow.reduce_stream(
            stream,
            _collect,
            no_texts,
            value_type=list[str],
            idle_timeout=timedelta(0),
            timeout=timedelta(seconds=10),
        )
        return TypedFoldResult(
            summary=summary.value,
            summary_type=type(summary.value).__name__,
            texts=texts.value,
            texts_type=type(texts.value).__name__,
        )


class TrackingStore(RecordingStreamStore):
    """A memory Store that knows whether a read waits, and whether one was cancelled."""

    def __init__(self) -> None:
        super().__init__(MemoryStreamStore())
        self.active_reads = 0
        self.cancelled_reads = 0

    async def read_records(self, request: ReadRecordsRequest) -> ReadRecordsResponse:
        self.active_reads += 1
        try:
            return await super().read_records(request)
        except asyncio.CancelledError:
            self.cancelled_reads += 1
            raise
        finally:
            self.active_reads -= 1


def _tokens(count: int, *, prefix: str = "t") -> list[Token]:
    return [Token("text", f"{_BODY_MARK}-{prefix}{index}") for index in range(count)]


def _done(text: str = "done") -> Token:
    return Token("done", f"{_BODY_MARK}-{text}")


async def _new_stream(
    client: Client,
    store: Any = None,
    data_converter: DataConverter | None = None,
) -> tuple[Client, Any, str]:
    store = store or MemoryStreamStore()
    config = client.config()
    config["stream_config"] = StreamConfig(stores=[store])
    if data_converter is not None:
        config["data_converter"] = data_converter
    stream_client = Client(**config)
    stream_id = f"stream-{uuid.uuid4()}"
    await stream_client.create_stream(id=stream_id, body_type=Token)
    return stream_client, store, stream_id


async def _append(stream_client: Client, stream_id: str, *tokens: Token) -> None:
    stream = stream_client.get_stream(StreamRef(id=stream_id), body_type=Token)
    await stream.append(*tokens)


async def _append_over(
    stream_client: Client, stream_id: str, tokens: list[Token], seconds: float
) -> float:
    """Append ``tokens`` one by one over ``seconds``, and give the time of the last."""
    last = time.monotonic()
    for token in tokens:
        await asyncio.sleep(seconds / len(tokens))
        last = time.monotonic()
        await _append(stream_client, stream_id, token)
    return last


def _texts(tokens: list[Token]) -> list[str]:
    return [token.text for token in tokens]


def _markers(history: WorkflowHistory) -> list[str]:
    return [
        event.marker_recorded_event_attributes.marker_name
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    ]


def _event_counts(history: WorkflowHistory) -> dict[str, int]:
    counts = collections.Counter(
        EventType.Name(event.event_type).removeprefix("EVENT_TYPE_")
        for event in history.events
    )
    return dict(sorted(counts.items()))


def _assert_no_bodies(history: WorkflowHistory) -> None:
    # The Workflow returns the bodies, so only its completion event has them.
    events = [
        event
        for event in history.events
        if event.event_type != EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED
    ]
    assert len(events) == len(history.events) - 1
    # The JSON has payload data in base64, so the raw events are searched too.
    assert _BODY_MARK not in WorkflowHistory(history.workflow_id, events).to_json()
    for event in events:
        assert _BODY_MARK.encode() not in event.SerializeToString()


async def _start_read(
    stream_client: Client,
    args: ReadArgs,
    task_queue: str,
    **kwargs: Any,
) -> WorkflowHandle[ReadUntilKindWorkflow, ReadResult]:
    return await stream_client.start_workflow(
        ReadUntilKindWorkflow.run,
        args,
        id=f"workflow-{uuid.uuid4()}",
        task_queue=task_queue,
        **kwargs,
    )


_RUNNERS = pytest.mark.parametrize(
    "runner",
    [SandboxedWorkflowRunner(), UnsandboxedWorkflowRunner()],
    ids=["sandboxed", "unsandboxed"],
)


async def _run_match(
    client: Client,
    runner: WorkflowRunner = SandboxedWorkflowRunner(),
    data_converter: DataConverter | None = None,
) -> tuple[Client, Any, WorkflowHandle[Any, ReadResult], ReadResult, list[Token]]:
    stream_client, store, stream_id = await _new_stream(
        client, data_converter=data_converter
    )
    tokens = [*_tokens(4), _done()]
    await _append(stream_client, stream_id, *tokens)
    async with new_worker(
        stream_client, ReadUntilKindWorkflow, workflow_runner=runner
    ) as worker:
        handle = await _start_read(
            stream_client, ReadArgs(stream_id), worker.task_queue
        )
        return stream_client, store, handle, await handle.result(), tokens


@_RUNNERS
async def test_read_until_match(client: Client, runner: WorkflowRunner):
    _, _, handle, result, tokens = await _run_match(client, runner)
    assert result == ReadResult(
        texts=_texts(tokens),
        stop="MATCH",
        done=False,
        last_offset=result.last_offset,
        typed=True,
    )
    assert result.last_offset
    history = await handle.fetch_history()
    assert _markers(history) == ["core_local_activity"]
    _assert_no_bodies(history)


async def test_read_waits_for_records(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [*_tokens(4), _done()]
    async with new_worker(stream_client, ReadUntilKindWorkflow) as worker:
        handle = await _start_read(
            stream_client, ReadArgs(stream_id), worker.task_queue
        )
        await _append_over(stream_client, stream_id, tokens, 1.0)
        result = await handle.result()
    assert result.texts == _texts(tokens)
    assert result.stop == "MATCH"
    assert _markers(await handle.fetch_history()) == ["core_local_activity"]


async def _wait_for_signal_delivery(handle: WorkflowHandle[Any, Any]) -> None:
    async def delivered() -> None:
        events = (await handle.fetch_history()).events
        signaled = [
            index
            for index, event in enumerate(events)
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
        ]
        assert signaled
        assert any(
            event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED
            for event in events[signaled[0] :]
        )

    await assert_eventually(delivered)


@_RUNNERS
async def test_read_predicate_reads_workflow_state(
    client: Client, runner: WorkflowRunner
):
    stream_client, store, stream_id = await _new_stream(client, TrackingStore())
    first = _tokens(2)
    wanted = Token("text", f"{_BODY_MARK}-wanted")
    await _append(stream_client, stream_id, *first)
    async with new_worker(
        stream_client, ReadUntilWantedWorkflow, workflow_runner=runner
    ) as worker:
        handle = await stream_client.start_workflow(
            ReadUntilWantedWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            task_timeout=timedelta(seconds=2),
        )

        async def read_waits() -> None:
            assert store.active_reads == 1

        await assert_eventually(read_waits)
        await handle.signal(ReadUntilWantedWorkflow.want, wanted.text)
        # A Workflow Task heartbeat brings the Signal while the Local Activity reads.
        await _wait_for_signal_delivery(handle)
        await asyncio.sleep(0.5)
        await _append(stream_client, stream_id, wanted, *_tokens(2, prefix="after"))
        result = await handle.result()
    assert result.texts == _texts([*first, wanted])
    assert result.stop == "MATCH"


async def test_read_replays_with_replayer(client: Client):
    _, store, handle, result, _ = await _run_match(client)
    run_id = handle.result_run_id
    assert run_id
    first_run = StreamReadState.results.pop(run_id)
    replay_store = RecordingStreamStore(store)
    StreamReadState.until_calls = 0
    await Replayer(
        workflows=[ReadUntilKindWorkflow],
        stream_config=StreamConfig(stores=[replay_store]),
    ).replay_workflow(await handle.fetch_history())
    assert StreamReadState.results.pop(run_id) == first_run
    assert first_run[0] == tuple(result.texts)
    assert replay_store.operations == ["ReadRecords"]
    assert StreamReadState.until_calls == 0


async def test_read_replays_on_a_worker_without_cache(client: Client):
    stream_client, store, stream_id = await _new_stream(
        client, RecordingStreamStore(MemoryStreamStore())
    )
    tokens = [
        Token("text", f"{_BODY_MARK}-{text}") for text in ["a", "m1", "b", "m2", "c"]
    ]
    await _append(stream_client, stream_id, *tokens)
    async with new_worker(
        stream_client, TwoReadsWorkflow, max_cached_workflows=0
    ) as worker:
        handle = await stream_client.start_workflow(
            TwoReadsWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == _texts(tokens[:4])
    history = await handle.fetch_history()
    assert _markers(history) == ["core_local_activity", "core_local_activity"]
    # Two reads, plus a replay of the first read before the Workflow Task of the second.
    reads = store.operations.count("ReadRecords")
    print(f"\nWorker without cache: {reads} ReadRecords calls")
    assert reads >= 3


async def test_read_replay_fails_when_records_are_gone(client: Client):
    _, _, handle, _, _ = await _run_match(client)
    history = await handle.fetch_history()
    with pytest.raises(Exception) as err:
        await Replayer(
            workflows=[ReadUntilKindWorkflow],
            stream_config=StreamConfig(stores=[MemoryStreamStore()]),
        ).replay_workflow(history)
    stream_id = history.events[0].workflow_execution_started_event_attributes
    assert "can't read the Stream" in str(err.value)
    assert f"'{_stream_id_of(stream_id)}'" in str(err.value)


def _stream_id_of(started: Any) -> str:
    return DataConverter.default.payload_converter.from_payloads(
        started.input.payloads, [ReadArgs]
    )[0].stream_id


async def test_read_survives_a_failed_workflow_task(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [*_tokens(3), _done()]
    await _append(stream_client, stream_id, *tokens)
    StreamReadState.fail_next_task = True
    async with new_worker(stream_client, FailTaskOnceAfterReadWorkflow) as worker:
        handle = await stream_client.start_workflow(
            FailTaskOnceAfterReadWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await handle.result()
    assert result.texts == _texts(tokens)
    assert result.stop == "MATCH"
    counts = _event_counts(await handle.fetch_history())
    assert counts["WORKFLOW_TASK_FAILED"] == 1
    assert counts["MARKER_RECORDED"] == 1


async def test_read_timeout_gives_records_so_far(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    first = _tokens(3)
    rest = [*_tokens(2, prefix="rest"), _done()]
    await _append(stream_client, stream_id, *first)
    async with new_worker(stream_client, TimeoutThenReadWorkflow) as worker:
        handle = await stream_client.start_workflow(
            TimeoutThenReadWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await asyncio.sleep(1.5)
        await _append(stream_client, stream_id, *rest)
        timed_out, matched = await handle.result()
    assert timed_out.texts == _texts(first)
    assert timed_out.stop == "TIMEOUT" and not timed_out.done
    assert matched.texts == _texts(rest)
    assert matched.stop == "MATCH"


async def test_read_stops_at_max_records(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = _tokens(10)
    await _append(stream_client, stream_id, *tokens)
    async with new_worker(stream_client, ReadUntilKindWorkflow) as worker:
        handle = await _start_read(
            stream_client,
            ReadArgs(stream_id, until_kind=None, max_records=3),
            worker.task_queue,
        )
        result = await handle.result()
    assert result.texts == _texts(tokens[:3])
    assert result.stop == "MAX_RECORDS" and not result.done


async def test_read_of_closed_stream_is_done(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = _tokens(4)
    await _append(stream_client, stream_id, *tokens)
    await stream_client.get_stream(StreamRef(id=stream_id), body_type=Token).close()
    async with new_worker(stream_client, ReadUntilKindWorkflow) as worker:
        handle = await _start_read(
            stream_client, ReadArgs(stream_id), worker.task_queue
        )
        result = await handle.result()
    assert result.texts == _texts(tokens)
    assert result.stop == "DONE" and result.done


async def test_read_with_payload_codec(client: Client):
    data_converter = DataConverter(payload_codec=SimpleCodec())
    StreamReadState.until_calls = 0
    _, store, handle, result, tokens = await _run_match(
        client, data_converter=data_converter
    )
    assert result.texts == _texts(tokens)
    assert result.typed and result.stop == "MATCH"
    # The predicate saw each Record decoded, or its type check would have raised.
    assert StreamReadState.until_calls == len(tokens)
    run_id = handle.result_run_id
    assert run_id
    first_run = StreamReadState.results.pop(run_id)
    history = await handle.fetch_history()
    marker = next(
        event.marker_recorded_event_attributes
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    )
    assert marker.details["result"].payloads[0].metadata["simple-codec"] == b"true"
    await Replayer(
        workflows=[ReadUntilKindWorkflow],
        data_converter=data_converter,
        stream_config=StreamConfig(stores=[store]),
    ).replay_workflow(history)
    assert StreamReadState.results.pop(run_id) == first_run


async def _cancel_waiting_read(client: Client, task_timeout: timedelta | None) -> float:
    """Cancel a Workflow while its read waits, and give the seconds until it ends."""
    stream_client, store, stream_id = await _new_stream(client, TrackingStore())
    async with new_worker(stream_client, ReadUntilKindWorkflow) as worker:
        handle = await _start_read(
            stream_client,
            ReadArgs(stream_id, timeout_seconds=30),
            worker.task_queue,
            task_timeout=task_timeout,
        )

        async def read_waits() -> None:
            assert store.active_reads == 1

        await assert_eventually(read_waits)
        start = time.monotonic()
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as err:
            await handle.result()
        seconds = time.monotonic() - start
        assert isinstance(err.value.cause, CancelledError)

        async def read_stopped() -> None:
            assert store.active_reads == 0
            assert store.cancelled_reads == 1

        await assert_eventually(read_stopped)
    print(f"\nCancel with task_timeout={task_timeout}: ended after {seconds:.1f} s")
    return seconds


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The cancel reaches the Workflow only at the next Workflow Task heartbeat, "
        "about 8 s with the default task timeout of 10 s."
    ),
)
async def test_read_cancel(client: Client):
    assert await _cancel_waiting_read(client, None) < 5


async def test_read_cancel_with_short_task_timeout(client: Client):
    assert await _cancel_waiting_read(client, timedelta(seconds=2)) < 5


async def test_read_predicate_that_raises(client: Client):
    assert await _failing_predicate(client, "raise") == (
        f"ValueError: bad token {_BODY_MARK}-t0"
    )


async def _failing_predicate(
    client: Client,
    predicate: str,
    runner: WorkflowRunner = SandboxedWorkflowRunner(),
) -> str:
    stream_client, _, stream_id = await _new_stream(client)
    await _append(stream_client, stream_id, *_tokens(1))
    async with new_worker(
        stream_client, FailingPredicateWorkflow, workflow_runner=runner
    ) as worker:
        return await stream_client.execute_workflow(
            FailingPredicateWorkflow.run,
            args=[stream_id, predicate],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


async def test_read_without_stream_config_fails_the_task(client: Client):
    async with new_worker(client, ReadUntilKindWorkflow) as worker:
        handle = await _start_read(
            client,
            ReadArgs(f"stream-{uuid.uuid4()}"),
            worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        await assert_task_fail_eventually(
            handle,
            message_contains=(
                "The client of this Worker has no Stream Store. Give one in "
                "stream_config=StreamConfig(stores=[...])."
            ),
        )
        await handle.terminate()


@_RUNNERS
async def test_read_predicate_cannot_call_workflow_functions(
    client: Client, runner: WorkflowRunner
):
    # The predicate runs on the Worker's event loop, where no Workflow runs.
    assert await _failing_predicate(client, "workflow_now", runner) == (
        "_NotInWorkflowEventLoopError: Not in workflow event loop"
    )


async def test_read_predicate_in_sandbox_keeps_its_restrictions(client: Client):
    sandboxed = await _failing_predicate(client, "datetime_now")
    assert sandboxed.startswith("RestrictedWorkflowAccessError: ")
    unsandboxed = await _failing_predicate(
        client, "datetime_now", UnsandboxedWorkflowRunner()
    )
    assert unsandboxed == "no error"


async def _run_flowing_read(
    client: Client, *, task_timeout: timedelta | None = None, seconds: float = 2.0
) -> tuple[WorkflowHistory, float]:
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [*_tokens(39), _done()]
    async with new_worker(stream_client, ReadUntilKindWorkflow) as worker:
        handle = await _start_read(
            stream_client,
            ReadArgs(stream_id, timeout_seconds=30),
            worker.task_queue,
            task_timeout=task_timeout,
        )
        appended = await _append_over(stream_client, stream_id, tokens, seconds)
        result = await handle.result()
        latency = time.monotonic() - appended
    assert result.texts == _texts(tokens)
    return await handle.fetch_history(), latency


async def test_measure_flowing_read(client: Client):
    history, _ = await _run_flowing_read(client)
    print(f"\nA. 40 Records over 2 s, History events: {_event_counts(history)}")


async def test_measure_idle_wait(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    async with new_worker(stream_client, ReadUntilKindWorkflow) as worker:
        handle = await _start_read(
            stream_client,
            ReadArgs(stream_id, timeout_seconds=30),
            worker.task_queue,
            task_timeout=timedelta(seconds=2),
        )
        await asyncio.sleep(6)
        await _append(stream_client, stream_id, _done())
        await handle.result()
    counts = _event_counts(await handle.fetch_history())
    print(f"\nB. Idle wait of 6 s, History events: {counts}")
    print(f"B. Workflow Tasks: {counts['WORKFLOW_TASK_COMPLETED']} completed")


async def test_measure_match_latency(client: Client):
    latencies = []
    for _ in range(5):
        _, latency = await _run_flowing_read(client)
        latencies.append(latency)
    print(
        "\nC. Append of the match to the end of the Workflow: "
        + ", ".join(f"{latency * 1000:.0f} ms" for latency in latencies)
    )


async def _run_workflow(
    stream_client: Client,
    workflow_class: type,
    *args: Any,
    worker_options: dict[str, Any] | None = None,
    **start_options: Any,
) -> tuple[WorkflowHandle[Any, Any], Any]:
    run = getattr(workflow_class, "run")
    async with new_worker(
        stream_client, workflow_class, **(worker_options or {})
    ) as worker:
        handle = await stream_client.start_workflow(
            run,
            args=list(args),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            **start_options,
        )
        return handle, await handle.result()


async def test_read_idle_timeout(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = _tokens(3)
    await _append(stream_client, stream_id, *tokens)
    start = time.monotonic()
    _, result = await _run_workflow(
        stream_client,
        ReadUntilKindWorkflow,
        ReadArgs(stream_id, idle_timeout_seconds=1, timeout_seconds=30),
    )
    seconds = time.monotonic() - start
    assert result.texts == _texts(tokens)
    assert result.stop == "IDLE" and not result.done
    assert 1 <= seconds < 3


async def test_read_idle_timeout_zero(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    empty_id = f"stream-{uuid.uuid4()}"
    await stream_client.create_stream(id=empty_id, body_type=Token)
    tokens = _tokens(3)
    await _append(stream_client, stream_id, *tokens)
    for read_id, expected in [(stream_id, _texts(tokens)), (empty_id, [])]:
        start = time.monotonic()
        _, result = await _run_workflow(
            stream_client,
            ReadUntilKindWorkflow,
            ReadArgs(read_id, idle_timeout_seconds=0, timeout_seconds=30),
        )
        assert result.texts == expected
        assert result.stop == "IDLE"
        assert time.monotonic() - start < 1


async def _record_sizes(store: Any, stream_id: str) -> list[int]:
    response = await store.read_records(
        ReadRecordsRequest(ref=StreamRefMessage(id=stream_id, store=store.name))
    )
    return [record.ByteSize() for record in response.records]


@pytest.mark.parametrize(
    "stop",
    ["MATCH", "DONE", "IDLE", "TIMEOUT", "MAX_RECORDS", "MAX_BYTES"],
)
async def test_read_stop_reason(client: Client, stop: str):
    stream_client, store, stream_id = await _new_stream(client)
    tokens = _tokens(5)
    if stop == "MATCH":
        tokens[2] = _done()
    await _append(stream_client, stream_id, *tokens)
    if stop == "DONE":
        await stream_client.get_stream(StreamRef(id=stream_id), body_type=Token).close()
    if stop == "MAX_BYTES":
        sizes = await _record_sizes(store, stream_id)
        # The third Record passes the cap by one byte.
        cap = sum(sizes[:3]) - 1
        _, (first, second) = await _run_workflow(
            stream_client, MaxBytesThenNextWorkflow, stream_id, cap
        )
        assert first.texts == _texts(tokens[:2])
        assert first.stop == "MAX_BYTES"
        assert second.texts == _texts(tokens[2:3])
        # A cap below the first Record still gives that Record.
        _, (alone, _) = await _run_workflow(
            stream_client, MaxBytesThenNextWorkflow, stream_id, 1
        )
        assert alone.texts == _texts(tokens[:1])
        assert alone.stop == "MAX_BYTES"
        return
    args = {
        "MATCH": ReadArgs(stream_id),
        "DONE": ReadArgs(stream_id),
        "IDLE": ReadArgs(stream_id, idle_timeout_seconds=0.5, timeout_seconds=30),
        "TIMEOUT": ReadArgs(stream_id, timeout_seconds=1),
        "MAX_RECORDS": ReadArgs(stream_id, max_records=3),
    }[stop]
    _, result = await _run_workflow(stream_client, ReadUntilKindWorkflow, args)
    assert result.stop == stop
    expected = {"MATCH": tokens[:3], "MAX_RECORDS": tokens[:3]}.get(stop, tokens)
    assert result.texts == _texts(expected)
    assert result.done == (stop == "DONE")


async def test_reduce_joins_text_and_replays_without_store(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [*_tokens(3), _done(), *_tokens(2, prefix="after")]
    await _append(stream_client, stream_id, *tokens)
    handle, result = await _run_workflow(
        stream_client, JoinTextWorkflow, ReduceArgs(stream_id)
    )
    assert result == ReduceResult(
        value="".join(_texts(tokens[:4])),
        count=4,
        stop="MATCH",
        done=False,
        last_offset=result.last_offset,
    )
    history = await handle.fetch_history()
    assert _markers(history) == ["core_local_activity"]
    run_id = handle.result_run_id
    assert run_id
    first_run = StreamReadState.results.pop(run_id)
    empty_store = RecordingStreamStore(MemoryStreamStore())
    StreamReadState.function_calls = 0
    StreamReadState.until_calls = 0
    await Replayer(
        workflows=[JoinTextWorkflow],
        stream_config=StreamConfig(stores=[empty_store]),
    ).replay_workflow(history)
    assert StreamReadState.results.pop(run_id) == first_run
    assert empty_store.operations == []
    assert StreamReadState.function_calls == 0
    assert StreamReadState.until_calls == 0


async def test_reduce_until_sees_the_value(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    texts = ["x", "ta", "g!", "y"]
    await _append(stream_client, stream_id, *[Token("text", text) for text in texts])
    _, result = await _run_workflow(
        stream_client, JoinTextWorkflow, ReduceArgs(stream_id, until="tag")
    )
    assert result.value == "xtag!"
    assert result.count == 3
    assert result.stop == "MATCH"


async def test_reduce_on_a_worker_without_cache(client: Client):
    stream_client, store, stream_id = await _new_stream(
        client, RecordingStreamStore(MemoryStreamStore())
    )
    texts = ["a", "m1", "b", "m2", "c"]
    await _append(stream_client, stream_id, *[Token("text", text) for text in texts])
    handle, (first, second) = await _run_workflow(
        stream_client,
        TwoFoldsWorkflow,
        stream_id,
        worker_options={"max_cached_workflows": 0},
    )
    assert (first.value, first.count, first.stop) == ("am1", 2, "MATCH")
    assert (second.value, second.count, second.stop) == ("bm2", 2, "MATCH")
    assert _markers(await handle.fetch_history()) == ["core_local_activity"] * 2
    # A replay of the first fold takes its value from History.
    assert store.operations.count("ReadRecords") == 2


async def test_reduce_function_that_raises(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    await _append(stream_client, stream_id, *_tokens(1))
    _, result = await _run_workflow(stream_client, FailingFunctionWorkflow, stream_id)
    assert result == f"ValueError: bad fold after '' at {_BODY_MARK}-t0"


async def test_reduce_typed_values(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [*_tokens(2), _done()]
    await _append(stream_client, stream_id, *tokens)
    _, result = await _run_workflow(stream_client, TypedFoldWorkflow, stream_id)
    assert result == TypedFoldResult(
        summary=Summary(3, ["text", "text", "done"]),
        summary_type="Summary",
        texts=_texts(tokens),
        texts_type="list",
    )


async def test_read_spans_heartbeats_on_a_worker_without_cache(client: Client):
    stream_client, store, stream_id = await _new_stream(client, TrackingStore())
    first = _tokens(2)
    await _append(stream_client, stream_id, *first)
    async with new_worker(
        stream_client, ReadUntilKindWorkflow, max_cached_workflows=0
    ) as worker:
        handle = await _start_read(
            stream_client,
            ReadArgs(stream_id, timeout_seconds=30),
            worker.task_queue,
            task_timeout=timedelta(seconds=2),
        )
        # The registry keeps the entry of the read at every sample while it waits.
        samples = []
        for _ in range(16):
            await asyncio.sleep(0.25)
            samples.append(
                any(
                    run_id == handle.result_run_id
                    for run_id, _ in _PENDING_STREAM_READS.keys()
                )
            )
        await _append(stream_client, stream_id, _done())
        result = await handle.result()
    counts = _event_counts(await handle.fetch_history())
    reads = store.operations.count("ReadRecords")
    print(
        f"\nHeartbeats without cache: {counts}, ReadRecords calls {reads}, "
        f"cancelled {store.cancelled_reads}, entry in {sum(samples)} of "
        f"{len(samples)} samples"
    )
    assert result.texts == _texts([*first, _done()])
    assert result.stop == "MATCH"
    # Heartbeats happened, and the one Local Activity ran through them uncancelled.
    assert counts["WORKFLOW_TASK_COMPLETED"] >= 3
    assert reads == 2
    assert store.cancelled_reads == 0
    assert all(samples)
    assert not any(
        run_id == handle.result_run_id for run_id, _ in _PENDING_STREAM_READS.keys()
    )


async def _signal_wait(client: Client) -> float:
    stream_client, _, stream_id = await _new_stream(client)
    async with new_worker(stream_client, ReadUntilWantedWorkflow) as worker:
        handle = await stream_client.start_workflow(
            ReadUntilWantedWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await asyncio.sleep(1)
        await handle.signal(ReadUntilWantedWorkflow.want, "never")

        async def delivered() -> float:
            events = (await handle.fetch_history()).events
            signals = [
                event
                for event in events
                if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED
            ]
            assert signals
            signaled = signals[0]
            started = [
                event
                for event in events
                if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED
                and event.event_id > signaled.event_id
            ]
            assert started
            return (
                started[0].event_time.ToDatetime() - signaled.event_time.ToDatetime()
            ).total_seconds()

        seconds = await assert_eventually(delivered, timeout=timedelta(seconds=15))
        await handle.terminate()
    return seconds


@pytest.mark.timeout(120)
async def test_measure_signal_wait(client: Client):
    waits = [await _signal_wait(client) for _ in range(3)]
    print(
        "\nD. Signal to the next Workflow Task Started while a read waits: "
        + ", ".join(f"{wait:.2f} s" for wait in waits)
    )


async def test_measure_flowing_fold(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [*_tokens(39), _done()]
    async with new_worker(stream_client, JoinTextWorkflow) as worker:
        handle = await stream_client.start_workflow(
            JoinTextWorkflow.run,
            ReduceArgs(stream_id, timeout_seconds=30),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await _append_over(stream_client, stream_id, tokens, 2.0)
        result = await handle.result()
    assert result.count == 40
    history = await handle.fetch_history()
    marker = next(
        event.marker_recorded_event_attributes
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    )
    payload = marker.details["result"].payloads[0]
    print(f"\nE. 40 Records folded over 2 s, History events: {_event_counts(history)}")
    print(
        f"E. Marker payload: {payload.ByteSize()} bytes, data {len(payload.data)} "
        f"bytes, folded value {len(result.value)} characters"
    )
