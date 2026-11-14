"""Tests of the take, filter and reduce calls of Workflow code."""

from __future__ import annotations

import asyncio
import collections
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import pytest

from temporalio import workflow
from temporalio.api.common.v1 import Payload, Payloads
from temporalio.api.enums.v1 import EventType, StreamErrorReason
from temporalio.api.stream.v1 import StreamRef as StreamRefMessage
from temporalio.api.streamservice.v1 import ReadRecordsRequest, ReadRecordsResponse
from temporalio.client import (
    Client,
    StreamConfig,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowHistory,
)
from temporalio.client._stream_read import _PENDING_STREAM_CALLS
from temporalio.contrib.streams import BufferedAppender
from temporalio.converter import DataConverter, PayloadCodec
from temporalio.exceptions import ApplicationError, CancelledError, StreamError
from temporalio.nexus.system.stream_service import (
    ProducerKindLocalActivity,
    StreamRead,
    StreamReadStop,
    StreamRecord,
    StreamRef,
)
from temporalio.nexus.system.stream_service.workflow._resources.stream import (
    Stream as WorkflowStream,
)
from temporalio.testing import MemoryStreamStore
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

# The Workflows and the test share the values of `StreamReadState`, so the sandbox
# must not load its own copy of the module.
with workflow.unsafe.imports_passed_through():
    from tests.helpers import assert_eventually, new_worker
    from tests.helpers.streams import (
        DictStreamStore,
        RecordingStreamStore,
        StreamReadState,
    )

# Every body carries this text, so a search of History shows whether a body is readable.
_BODY_MARK = "secret-body"


@dataclass
class Token:
    kind: str
    text: str


@dataclass
class CallArgs:
    stream_id: str
    call: str = "take"
    until_kind: str | None = "done"
    timeout_seconds: float = 10.0
    idle_timeout_seconds: float | None = None
    max_records: int | None = None
    max_bytes: int | None = None
    from_offset: str | None = None


@dataclass
class CallResult:
    texts: list[str]
    """The texts of the Records of a take or a filter, or the value of a fold."""
    count: int
    stop: str
    done: bool
    last_offset: str | None
    typed: bool


def _seconds(value: float | None) -> timedelta | None:
    return None if value is None else timedelta(seconds=value)


def _stream(stream_id: str) -> WorkflowStream[Token]:
    return workflow.get_stream(StreamRef(id=stream_id), body_type=Token)


def _not_skipped(record: StreamRecord[Token]) -> bool:
    return record.body.kind != "skip"


def _collect(value: list[str], record: StreamRecord[Token]) -> list[str]:
    StreamReadState.function_calls += 1
    return [*value, record.body.text]


def _read_result(read: StreamRead[Token]) -> CallResult:
    return CallResult(
        texts=[record.body.text for record in read.records],
        count=len(read.records),
        stop=read.stop.name,
        done=read.done,
        last_offset=read.last_offset.hex() if read.last_offset else None,
        typed=all(
            isinstance(record, StreamRecord) and isinstance(record.body, Token)
            for record in read.records
        ),
    )


def _keep(result: CallResult) -> CallResult:
    # A replay gives no result to the test, so the Workflow keeps it here.
    StreamReadState.results[workflow.info().run_id] = (
        tuple(result.texts),
        result.count,
        result.stop,
        result.done,
        result.last_offset,
    )
    return result


async def _call(args: CallArgs, stream: Any = None) -> CallResult:
    """Run the call of ``args``. Client and Activity code give a client Stream."""
    stream = stream or _stream(args.stream_id)
    until_kind = args.until_kind

    def until(record: StreamRecord[Token]) -> bool:
        StreamReadState.until_calls += 1
        assert isinstance(record.body, Token)
        return record.body.kind == until_kind

    def fold_until(value: list[str], record: StreamRecord[Token]) -> bool:
        del value
        StreamReadState.until_calls += 1
        return record.body.kind == until_kind

    timeout = timedelta(seconds=args.timeout_seconds)
    from_offset = bytes.fromhex(args.from_offset) if args.from_offset else None
    idle_timeout = _seconds(args.idle_timeout_seconds)
    # The arguments go by position, in the order that the generated methods use.
    if args.call == "take":
        read = await stream.take(
            timeout,
            until if until_kind else None,
            from_offset,
            idle_timeout,
            args.max_records,
            args.max_bytes,
        )
        return _read_result(read)
    if args.call == "filter":
        read = await stream.filter(
            _not_skipped,
            timeout,
            until if until_kind else None,
            from_offset,
            idle_timeout,
            args.max_records,
            args.max_bytes,
        )
        return _read_result(read)
    no_texts: list[str] = []
    reduce = await stream.reduce(
        _collect,
        no_texts,
        timeout,
        fold_until if until_kind else None,
        from_offset,
        idle_timeout,
        args.max_records,
        args.max_bytes,
    )
    return CallResult(
        texts=reduce.value,
        count=reduce.count,
        stop=reduce.stop.name,
        done=reduce.done,
        last_offset=reduce.last_offset.hex() if reduce.last_offset else None,
        typed=isinstance(reduce.value, list),
    )


@workflow.defn
class CallWorkflow:
    def __init__(self) -> None:
        self.result: CallResult | None = None

    @workflow.run
    async def run(self, args: CallArgs) -> CallResult:
        self.result = _keep(await _call(args))
        return self.result

    @workflow.query
    def texts(self) -> list[str]:
        assert self.result is not None
        return self.result.texts


@workflow.defn
class TakeUntilWantedWorkflow:
    def __init__(self) -> None:
        self.wanted: str | None = None

    @workflow.signal
    def want(self, text: str) -> None:
        self.wanted = text

    @workflow.run
    async def run(self, stream_id: str) -> CallResult:
        read = await _stream(stream_id).take(
            timedelta(seconds=20),
            until=lambda record: record.body.text == self.wanted,
            idle_timeout=timedelta(seconds=20),
        )
        return _read_result(read)


@workflow.defn
class TwoTakesWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        stream = _stream(stream_id)
        first = await stream.take(
            timedelta(seconds=10),
            until=lambda record: record.body.text.endswith("m1"),
        )
        await workflow.sleep(timedelta(milliseconds=1))
        second = await stream.take(
            timedelta(seconds=10),
            until=lambda record: record.body.text.endswith("m2"),
            from_offset=first.last_offset,
        )
        return [record.body.text for record in [*first.records, *second.records]]


@workflow.defn
class FailTaskOnceAfterTakeWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> CallResult:
        read = await _stream(stream_id).take(
            timedelta(seconds=10),
            until=lambda record: record.body.kind == "done",
        )
        # The failure comes in a later Workflow Task, so the retry replays the call.
        await workflow.sleep(timedelta(milliseconds=1))
        if StreamReadState.fail_next_task:
            StreamReadState.fail_next_task = False
            raise RuntimeError("Failed the Workflow Task once")
        return _read_result(read)


@workflow.defn
class FilterLoopWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> tuple[list[str], int]:
        stream = _stream(stream_id)
        found: list[str] = []
        offset: bytes | None = None
        calls = 0
        while True:
            calls += 1
            # A filter with `max_records=1` gives the next Record that it keeps.
            read = await stream.filter(
                lambda record: record.body.kind == "hit",
                timedelta(seconds=10),
                from_offset=offset,
                idle_timeout=timedelta(0),
                max_records=1,
            )
            found.extend(record.body.text for record in read.records)
            offset = read.last_offset
            if read.stop in (
                StreamReadStop.Done,
                StreamReadStop.Idle,
            ):
                return found, calls


@workflow.defn
class TakeLoopWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[tuple[int, str]]:
        stream = _stream(stream_id)
        pages: list[tuple[int, str]] = []
        offset: bytes | None = None
        while True:
            read = await stream.take(timedelta(seconds=10), from_offset=offset)
            pages.append((len(read.records), read.stop.name))
            offset = read.last_offset
            if read.stop is not StreamReadStop.MaxBytes:
                return pages


@workflow.defn
class OverLimitWorkflow:
    @workflow.run
    async def run(self, stream_id: str, call: str) -> str:
        stream = _stream(stream_id)
        try:
            if call == "take":
                await stream.take(timedelta(seconds=10), max_bytes=4 * 1024 * 1024)
            else:
                await stream.reduce(
                    lambda value, record: value + "x" * (1024 * 1024),
                    "",
                    timedelta(seconds=10),
                    max_records=1,
                )
        except ApplicationError as err:
            return f"{err.type} {err.non_retryable}: {err.message}"
        return "no error"


@workflow.defn
class TwoStreamsAtOnceWorkflow:
    @workflow.run
    async def run(self, first_id: str, second_id: str) -> list[str]:
        tasks = [
            asyncio.create_task(
                _stream(stream_id).filter(
                    lambda record: record.body.kind == "hit",
                    timedelta(seconds=10),
                    max_records=1,
                )
            )
            for stream_id in (first_id, second_id)
        ]
        # `workflow.wait` is the deterministic form of `asyncio.wait`.
        await workflow.wait(tasks)
        return [record.body.text for task in tasks for record in task.result().records]


@workflow.defn
class VariantWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        # A replay sets `variant` to change the code after the first run.
        variant = StreamReadState.variant
        if variant == "uuid":
            workflow.uuid4()
        if variant == "other_stream":
            stream_id += "-other"
        read = await _stream(stream_id).take(
            timedelta(seconds=10),
            from_offset=b"\x01" if variant == "other_offset" else None,
            idle_timeout=timedelta(0),
        )
        return [record.body.text for record in read.records]


async def _async_until(record: StreamRecord[Token]) -> bool:
    del record
    return True


def _until_that_awaits(record: StreamRecord[Token]) -> Any:
    del record
    return asyncio.sleep(0)


@workflow.defn
class RefusedFunctionWorkflow:
    @workflow.run
    async def run(self, stream_id: str, which: str) -> str:
        stream = _stream(stream_id)
        # The type checker refuses both functions, and the call refuses them at run time.
        until: Any = _async_until if which == "coroutine" else _until_that_awaits
        try:
            await stream.take(timedelta(seconds=10), until=until)
        except TypeError as err:
            return f"TypeError: {err}"
        except ApplicationError as err:
            return f"ApplicationError {err.type} {err.non_retryable}: {err.message}"
        return "no error"


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
            await _stream(stream_id).filter(
                _PREDICATES[predicate], timedelta(seconds=10)
            )
        except ApplicationError as err:
            return f"{err.type}: {err.message}"
        return "no error"


@dataclass
class Summary:
    count: int
    kinds: list[str]


def _summarize(value: Summary | None, record: StreamRecord[Token]) -> Summary:
    if value is None:
        value = Summary(0, [])
    return Summary(value.count + 1, [*value.kinds, record.body.kind])


def _texts_so_far(value: Sequence[str], record: StreamRecord[Token]) -> list[str]:
    return [*value, record.body.text]


@workflow.defn
class FoldTypesWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        stream = _stream(stream_id)
        quick = timedelta(seconds=10)
        now = timedelta(0)
        # The return annotation wins over an `initial` of `None`.
        annotated = await stream.reduce(_summarize, None, quick, idle_timeout=now)
        # With no annotation, the type of `initial` decides.
        of_initial = await stream.reduce(
            lambda value, record: Summary(value.count + 1, []),
            Summary(0, []),
            quick,
            idle_timeout=now,
        )
        # With no annotation and an `initial` of `None`, there is no type hint.
        untyped = await stream.reduce(
            lambda value, record: Summary(1, [record.body.kind]),
            cast(Any, None),
            quick,
            idle_timeout=now,
        )
        # The return annotation wins over the type of `initial` too.
        over_initial = await stream.reduce(
            _texts_so_far, cast(Any, ()), quick, idle_timeout=now
        )
        return [
            type(result.value).__name__
            for result in (annotated, of_initial, untyped, over_initial)
        ]


@workflow.defn
class FoldUntilValueWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> tuple[str, int, str]:
        result = await _stream(stream_id).reduce(
            lambda value, record: value + record.body.text,
            "",
            timedelta(seconds=10),
            until=lambda value, record: value.endswith("tag!"),
        )
        return result.value, result.count, result.stop.name


@workflow.defn
class FoldThatAppendsWorkflow:
    @workflow.run
    async def run(self, source_id: str, target_id: str) -> str:
        target = workflow.get_stream(StreamRef(id=target_id), body_type=str)
        appender = BufferedAppender(target)

        def append_upper(value: int, record: StreamRecord[Token]) -> int:
            appender.append(record.body.text.upper())
            return value + 1

        try:
            result = await _stream(source_id).reduce(
                append_upper,
                0,
                timedelta(seconds=10),
                idle_timeout=timedelta(0),
            )
        except StreamError as err:
            return StreamErrorReason.Name(err.reason)
        return f"{result.count} {result.stop.name}"


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


class XorCodec(PayloadCodec):
    """A codec that leaves no text of a payload readable."""

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload(metadata={"xor-codec": b"true"}, data=_xor(payload))
            for payload in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload.FromString(bytes(byte ^ 0x5A for byte in payload.data))
            for payload in payloads
        ]


def _xor(payload: Payload) -> bytes:
    return bytes(byte ^ 0x5A for byte in payload.SerializeToString())


def _tokens(count: int, *, prefix: str = "t", kind: str = "text") -> list[Token]:
    return [Token(kind, f"{_BODY_MARK}-{prefix}{index}") for index in range(count)]


def _done(text: str = "done") -> Token:
    return Token("done", f"{_BODY_MARK}-{text}")


def _with_store(
    client: Client, store: Any, data_converter: DataConverter | None = None
) -> Client:
    config = client.config()
    config["stream_config"] = StreamConfig(stores=[store])
    if data_converter is not None:
        config["data_converter"] = data_converter
    return Client(**config)


async def _new_stream(
    client: Client,
    store: Any = None,
    data_converter: DataConverter | None = None,
) -> tuple[Client, Any, str]:
    store = store or MemoryStreamStore()
    stream_client = _with_store(client, store, data_converter)
    stream_id = f"stream-{uuid.uuid4()}"
    await stream_client.create_stream(id=stream_id, body_type=Token)
    return stream_client, store, stream_id


async def _append(stream_client: Client, stream_id: str, *tokens: Token) -> None:
    stream = stream_client.get_stream(StreamRef(id=stream_id), body_type=Token)
    await stream.append(*tokens)


async def _close(stream_client: Client, stream_id: str) -> None:
    await stream_client.get_stream(StreamRef(id=stream_id), body_type=Token).close()


async def _offsets(stream_client: Client, stream_id: str) -> list[str]:
    stream = stream_client.get_stream(StreamRef(id=stream_id), body_type=Token)
    return [record.offset.hex() async for record in stream.iter_read_items()]


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


def _texts(tokens: Sequence[Token]) -> list[str]:
    return [token.text for token in tokens]


def _markers(history: WorkflowHistory) -> list[str]:
    return [
        event.marker_recorded_event_attributes.marker_name
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    ]


def _marker_payload(history: WorkflowHistory, index: int = 0) -> Payload:
    markers = [
        event.marker_recorded_event_attributes
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    ]
    return markers[index].details["result"].payloads[0]


def _event_counts(history: WorkflowHistory) -> dict[str, int]:
    counts = collections.Counter(
        EventType.Name(event.event_type).removeprefix("EVENT_TYPE_")
        for event in history.events
    )
    return dict(sorted(counts.items()))


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


async def _start_call(
    stream_client: Client, args: CallArgs, task_queue: str, **kwargs: Any
) -> WorkflowHandle[CallWorkflow, CallResult]:
    return await stream_client.start_workflow(
        CallWorkflow.run,
        args,
        id=f"workflow-{uuid.uuid4()}",
        task_queue=task_queue,
        **kwargs,
    )


_CALLS = pytest.mark.parametrize("call", ["take", "filter", "reduce"])
_STOPS = ["Match", "Done", "MaxRecords", "MaxBytes", "Idle", "Timeout"]

# The five Records of a stop test. The filter drops the Record of the kind "skip".
_STOP_TOKENS = [
    Token("text", f"{_BODY_MARK}-t0"),
    Token("skip", f"{_BODY_MARK}-s1"),
    Token("text", f"{_BODY_MARK}-t2"),
    Token("text", f"{_BODY_MARK}-t3"),
    Token("text", f"{_BODY_MARK}-t4"),
]


def _stop_case(
    call: str, stop: str
) -> tuple[list[Token], dict[str, Any], list[int], int]:
    """Give the Records, the arguments, the indexes of the result and of ``last_offset``."""
    tokens = list(_STOP_TOKENS)
    every = [0, 1, 2, 3, 4]
    kept = every if call != "filter" else [0, 2, 3, 4]
    if stop == "Match":
        tokens[3] = _done()
        return tokens, {}, [index for index in kept if index <= 3], 3
    if stop == "Done":
        return tokens, {"until_kind": None}, kept, 4
    if stop == "MaxRecords":
        return tokens, {"max_records": 2}, kept[:2], kept[1]
    if stop == "MaxBytes":
        # The first Record passes the cap and comes back, and the next kept one stops the
        # call. A filter looks at the dropped Record in between.
        return tokens, {"max_bytes": 1}, kept[:1], 1 if call == "filter" else 0
    if stop == "Idle":
        return tokens, {"idle_timeout_seconds": 0.3, "until_kind": None}, kept, 4
    return (
        tokens,
        {"timeout_seconds": 1, "idle_timeout_seconds": 30, "until_kind": None},
        kept,
        4,
    )


async def _check_stop(
    stream_client: Client,
    call: str,
    stop: str,
    run: Callable[[str, dict[str, Any]], Any],
) -> None:
    """Append the Records of a stop case, run the call, and check its result."""
    stream_id = f"stream-{uuid.uuid4()}"
    await stream_client.create_stream(id=stream_id, body_type=Token)
    tokens, options, indexes, last = _stop_case(call, stop)
    await _append(stream_client, stream_id, *tokens)
    if stop == "Done":
        await _close(stream_client, stream_id)
    offsets = await _offsets(stream_client, stream_id)
    result = await run(stream_id, options)
    texts = [tokens[index].text for index in indexes]
    if call == "reduce":
        # A fold gets every Record that it looks at.
        texts = [tokens[index].text for index in range(last + 1)]
    assert (result.stop, result.texts, result.last_offset) == (
        stop,
        texts,
        offsets[last],
    )
    assert result.count == len(texts)
    assert result.done == (stop == "Done")


@_CALLS
@pytest.mark.parametrize("stop", _STOPS)
async def test_call_stop_and_last_offset(client: Client, call: str, stop: str):
    stream_client = _with_store(client, MemoryStreamStore())

    async def run(stream_id: str, options: dict[str, Any]) -> CallResult:
        _, result = await _run_workflow(
            stream_client, CallWorkflow, CallArgs(stream_id, call=call, **options)
        )
        assert result.typed
        return result

    await _check_stop(stream_client, call, stop, run)


async def test_filter_loop_reads_to_the_end(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens: list[Token] = []
    for index in range(20):
        kind = "hit" if index % 4 == 1 else "text"
        tokens.append(Token(kind, f"{_BODY_MARK}-{kind}{index}"))
    await _append(stream_client, stream_id, *tokens)
    await _close(stream_client, stream_id)
    handle, (found, calls) = await _run_workflow(
        stream_client, FilterLoopWorkflow, stream_id
    )
    hits = [token.text for token in tokens if token.kind == "hit"]
    assert found == hits
    # One call for each of the 5 matches, and one that reaches the end.
    assert calls == 6
    history = await handle.fetch_history()
    assert len(_markers(history)) == 6
    print(
        f"\nC. A filter loop over 5 matches, History events: {_event_counts(history)}"
    )


async def test_filter_until_accepts_a_dropped_record(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [
        Token("text", f"{_BODY_MARK}-t0"),
        Token("skip", f"{_BODY_MARK}-s1"),
        Token("text", f"{_BODY_MARK}-t2"),
    ]
    await _append(stream_client, stream_id, *tokens)
    offsets = await _offsets(stream_client, stream_id)
    _, result = await _run_workflow(
        stream_client,
        CallWorkflow,
        CallArgs(stream_id, call="filter", until_kind="skip"),
    )
    assert result.texts == _texts(tokens[:1])
    assert result.stop == "Match"
    assert result.last_offset == offsets[1]


@_CALLS
async def test_replay_needs_no_stream_config_and_calls_no_store(
    client: Client, call: str
):
    store = RecordingStreamStore(MemoryStreamStore())
    stream_client, _, stream_id = await _new_stream(client, store)
    await _append(stream_client, stream_id, *_STOP_TOKENS[:3], _done())
    handle, _ = await _run_workflow(
        stream_client, CallWorkflow, CallArgs(stream_id, call=call)
    )
    run_id = handle.result_run_id
    assert run_id
    first_run = StreamReadState.results.pop(run_id)
    history = await handle.fetch_history()
    assert _markers(history) == ["core_local_activity"]
    StreamReadState.until_calls = 0
    StreamReadState.function_calls = 0
    operations = len(store.operations)
    await Replayer(workflows=[CallWorkflow]).replay_workflow(history)
    assert StreamReadState.results.pop(run_id) == first_run
    await Replayer(
        workflows=[CallWorkflow], stream_config=StreamConfig(stores=[store])
    ).replay_workflow(history)
    assert StreamReadState.results.pop(run_id) == first_run
    assert len(store.operations) == operations
    assert StreamReadState.until_calls == 0
    assert StreamReadState.function_calls == 0


async def test_query_after_the_stream_is_deleted(client: Client):
    store = DictStreamStore()
    stream_client, _, stream_id = await _new_stream(client, store)
    tokens = [*_tokens(3), _done()]
    await _append(stream_client, stream_id, *tokens)
    task_queue = f"tq-{uuid.uuid4()}"
    async with new_worker(stream_client, CallWorkflow, task_queue=task_queue):
        handle = await _start_call(
            stream_client, CallArgs(stream_id, idle_timeout_seconds=0), task_queue
        )
        assert (await handle.result()).texts == _texts(tokens)
    del store.streams[stream_id]
    # A new Worker has no cache, so the Query replays the whole History.
    async with new_worker(stream_client, CallWorkflow, task_queue=task_queue):
        assert await handle.query(CallWorkflow.texts) == _texts(tokens)
    assert stream_id not in store.streams


async def test_take_loop_over_max_bytes(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    # 40 Records of about 10 KiB pass the default cap of 256 KiB.
    tokens = [
        Token("text", f"{_BODY_MARK}-{index}-" + "x" * 10_000) for index in range(40)
    ]
    for start in range(0, len(tokens), 10):
        await _append(stream_client, stream_id, *tokens[start : start + 10])
    await _close(stream_client, stream_id)
    _, pages = await _run_workflow(stream_client, TakeLoopWorkflow, stream_id)
    assert [stop for _, stop in pages] == ["MaxBytes", "Done"]
    assert sum(count for count, _ in pages) == len(tokens)
    print(f"\nTake loop over 400 KiB: {pages}")


@pytest.mark.parametrize("call", ["take", "reduce"])
async def test_result_over_the_limit_raises_at_the_await(client: Client, call: str):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [Token("text", "x" * 20_000) for _ in range(60)]
    for start in range(0, len(tokens), 10):
        await _append(stream_client, stream_id, *tokens[start : start + 10])
    await _close(stream_client, stream_id)
    _, message = await _run_workflow(stream_client, OverLimitWorkflow, stream_id, call)
    prefix = f"StreamResultTooLarge True: The result of the {call} of the Stream "
    assert message.startswith(prefix + f"'{stream_id}' is ")
    assert "bytes encoded, over the limit of 1048576 bytes." in message


@pytest.mark.parametrize(
    "variant", ["other_stream", "other_offset", "uuid"], ids=lambda value: value
)
async def test_replay_checks_the_stream_and_offset_of_a_call(
    client: Client, variant: str
):
    stream_client, _, stream_id = await _new_stream(client)
    await _append(stream_client, stream_id, *_tokens(2))
    StreamReadState.variant = ""
    handle, _ = await _run_workflow(stream_client, VariantWorkflow, stream_id)
    history = await handle.fetch_history()
    StreamReadState.variant = variant
    try:
        result = await Replayer(workflows=[VariantWorkflow]).replay_workflow(
            history, raise_on_replay_failure=False
        )
    finally:
        StreamReadState.variant = ""
    if variant == "uuid":
        assert result.replay_failure is None
        return
    failure = str(result.replay_failure)
    # The Workflow code raised the SDK's non-determinism error, which failed the task.
    assert 'r#type: "NondeterminismError"' in failure
    assert "Stream call 1 of the Workflow is a take of the Stream" in failure
    assert f"History has a take of the Stream '{stream_id}' on the Store" in failure


async def test_payload_codec_hides_every_body_in_history(client: Client):
    data_converter = DataConverter(payload_codec=XorCodec())
    stream_client, _, stream_id = await _new_stream(
        client, data_converter=data_converter
    )
    await _append(stream_client, stream_id, *_tokens(3), _done())
    for call in ("take", "filter", "reduce"):
        handle, result = await _run_workflow(
            stream_client, CallWorkflow, CallArgs(stream_id, call=call)
        )
        assert result.count == 4 and result.stop == "Match"
        history = await handle.fetch_history()
        payload = _marker_payload(history)
        assert payload.metadata["xor-codec"] == b"true"
        assert _BODY_MARK not in history.to_json()
        for event in history.events:
            assert _BODY_MARK.encode() not in event.SerializeToString()
        await Replayer(
            workflows=[CallWorkflow], data_converter=data_converter
        ).replay_workflow(history)


@pytest.mark.parametrize("which", ["coroutine", "awaitable"])
async def test_function_must_be_plain(client: Client, which: str):
    stream_client, _, stream_id = await _new_stream(client)
    await _append(stream_client, stream_id, *_tokens(1))
    _, message = await _run_workflow(
        stream_client, RefusedFunctionWorkflow, stream_id, which
    )
    if which == "coroutine":
        assert message.startswith("TypeError: until must be a plain function")
    else:
        assert message == (
            "ApplicationError TypeError True: The until of a Stream call returned an "
            "awaitable. It must be a plain function that returns its answer."
        )


async def test_fold_value_types(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    await _append(stream_client, stream_id, *_tokens(2))
    _, names = await _run_workflow(stream_client, FoldTypesWorkflow, stream_id)
    assert names == ["Summary", "Summary", "dict", "list"]


async def test_fold_until_sees_the_value(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    texts = ["x", "ta", "g!", "y"]
    await _append(stream_client, stream_id, *[Token("text", text) for text in texts])
    _, result = await _run_workflow(stream_client, FoldUntilValueWorkflow, stream_id)
    assert list(result) == ["xtag!", 3, "Match"]


async def test_two_calls_on_two_streams_at_once(client: Client):
    stream_client, _, first_id = await _new_stream(client)
    second_id = f"stream-{uuid.uuid4()}"
    await stream_client.create_stream(id=second_id, body_type=Token)
    await _append(stream_client, first_id, *_tokens(2), Token("hit", "first-hit"))
    await _append(stream_client, second_id, Token("hit", "second-hit"))
    handle, texts = await _run_workflow(
        stream_client, TwoStreamsAtOnceWorkflow, first_id, second_id
    )
    assert texts == ["first-hit", "second-hit"]
    assert _markers(await handle.fetch_history()) == ["core_local_activity"] * 2


async def test_default_idle_stop_on_a_quiet_stream(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    start = time.monotonic()
    handle, result = await _run_workflow(
        stream_client, CallWorkflow, CallArgs(stream_id, timeout_seconds=30)
    )
    seconds = time.monotonic() - start
    assert result.stop == "Idle" and result.texts == []
    assert 1 <= seconds < 3
    counts = _event_counts(await handle.fetch_history())
    # The one Workflow Task held the call, so no heartbeat added a task.
    assert counts["WORKFLOW_TASK_COMPLETED"] == 1
    print(f"\nB. A quiet Stream with the default idle stop, History events: {counts}")


async def test_take_waits_for_records(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [*_tokens(4), _done()]
    async with new_worker(stream_client, CallWorkflow) as worker:
        handle = await _start_call(
            stream_client,
            CallArgs(stream_id, idle_timeout_seconds=5),
            worker.task_queue,
        )
        await _append_over(stream_client, stream_id, tokens, 1.0)
        result = await handle.result()
    assert result.texts == _texts(tokens)
    assert result.stop == "Match"
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


_RUNNERS = pytest.mark.parametrize(
    "runner",
    [SandboxedWorkflowRunner(), UnsandboxedWorkflowRunner()],
    ids=["sandboxed", "unsandboxed"],
)


@_RUNNERS
async def test_until_reads_workflow_state(client: Client, runner: WorkflowRunner):
    stream_client, store, stream_id = await _new_stream(client, TrackingStore())
    first = _tokens(2)
    wanted = Token("text", f"{_BODY_MARK}-wanted")
    await _append(stream_client, stream_id, *first)
    async with new_worker(
        stream_client, TakeUntilWantedWorkflow, workflow_runner=runner
    ) as worker:
        handle = await stream_client.start_workflow(
            TakeUntilWantedWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            task_timeout=timedelta(seconds=2),
        )

        async def read_waits() -> None:
            assert store.active_reads == 1

        await assert_eventually(read_waits)
        await handle.signal(TakeUntilWantedWorkflow.want, wanted.text)
        # A Workflow Task heartbeat brings the Signal while the Local Activity reads.
        await _wait_for_signal_delivery(handle)
        await asyncio.sleep(0.5)
        await _append(stream_client, stream_id, wanted, *_tokens(2, prefix="after"))
        result = await handle.result()
    assert result.texts == _texts([*first, wanted])
    assert result.stop == "Match"


async def test_calls_on_a_worker_without_cache(client: Client):
    stream_client, store, stream_id = await _new_stream(
        client, RecordingStreamStore(MemoryStreamStore())
    )
    tokens = [
        Token("text", f"{_BODY_MARK}-{text}") for text in ["a", "m1", "b", "m2", "c"]
    ]
    await _append(stream_client, stream_id, *tokens)
    handle, texts = await _run_workflow(
        stream_client,
        TwoTakesWorkflow,
        stream_id,
        worker_options={"max_cached_workflows": 0},
    )
    assert texts == _texts(tokens[:4])
    assert _markers(await handle.fetch_history()) == ["core_local_activity"] * 2
    # The Workflow Task of the second call replays the first from History.
    assert store.operations.count("ReadRecords") == 2


async def test_call_survives_a_failed_workflow_task(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [*_tokens(3), _done()]
    await _append(stream_client, stream_id, *tokens)
    StreamReadState.fail_next_task = True
    handle, result = await _run_workflow(
        stream_client, FailTaskOnceAfterTakeWorkflow, stream_id
    )
    assert result.texts == _texts(tokens)
    assert result.stop == "Match"
    counts = _event_counts(await handle.fetch_history())
    assert counts["WORKFLOW_TASK_FAILED"] == 1
    assert counts["MARKER_RECORDED"] == 1


async def _failing_predicate(
    client: Client,
    predicate: str,
    runner: WorkflowRunner = SandboxedWorkflowRunner(),
) -> str:
    stream_client, _, stream_id = await _new_stream(client)
    await _append(stream_client, stream_id, *_tokens(1))
    _, result = await _run_workflow(
        stream_client,
        FailingPredicateWorkflow,
        stream_id,
        predicate,
        worker_options={"workflow_runner": runner},
    )
    return result


async def test_predicate_that_raises(client: Client):
    assert await _failing_predicate(client, "raise") == (
        f"ValueError: bad token {_BODY_MARK}-t0"
    )


@_RUNNERS
async def test_predicate_cannot_call_workflow_functions(
    client: Client, runner: WorkflowRunner
):
    # The predicate runs on the Worker's event loop, where no Workflow runs.
    assert await _failing_predicate(client, "workflow_now", runner) == (
        "_NotInWorkflowEventLoopError: Not in workflow event loop"
    )


async def test_predicate_in_sandbox_keeps_its_restrictions(client: Client):
    sandboxed = await _failing_predicate(client, "datetime_now")
    assert sandboxed.startswith("RestrictedWorkflowAccessError: ")
    unsandboxed = await _failing_predicate(
        client, "datetime_now", UnsandboxedWorkflowRunner()
    )
    assert unsandboxed == "no error"


async def test_calls_without_stream_config_fail_the_workflow(client: Client):
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(client, CallWorkflow) as worker:
        for call in ("take", "filter", "reduce"):
            handle = await _start_call(
                client,
                CallArgs(stream_id, call=call),
                worker.task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            with pytest.raises(WorkflowFailureError) as err:
                await asyncio.wait_for(handle.result(), timeout=10)
            cause = err.value.cause
            assert isinstance(cause, ApplicationError)
            assert cause.type == "StreamConfigError"
            assert cause.non_retryable


async def _cancel_waiting_call(client: Client, task_timeout: timedelta | None) -> float:
    """Cancel a Workflow while its call waits, and give the seconds until it ends."""
    stream_client, store, stream_id = await _new_stream(client, TrackingStore())
    async with new_worker(stream_client, CallWorkflow) as worker:
        handle = await _start_call(
            stream_client,
            CallArgs(stream_id, timeout_seconds=30, idle_timeout_seconds=30),
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
async def test_cancel_of_a_long_wait(client: Client):
    assert await _cancel_waiting_call(client, None) < 5


async def test_cancel_with_short_task_timeout(client: Client):
    assert await _cancel_waiting_call(client, timedelta(seconds=2)) < 5


async def test_call_spans_heartbeats_on_a_worker_without_cache(client: Client):
    stream_client, store, stream_id = await _new_stream(client, TrackingStore())
    first = _tokens(2)
    await _append(stream_client, stream_id, *first)
    async with new_worker(
        stream_client, CallWorkflow, max_cached_workflows=0
    ) as worker:
        handle = await _start_call(
            stream_client,
            CallArgs(stream_id, timeout_seconds=30, idle_timeout_seconds=30),
            worker.task_queue,
            task_timeout=timedelta(seconds=2),
        )
        # The registry keeps the entry of the call at every sample while it waits.
        samples = []
        for _ in range(16):
            await asyncio.sleep(0.25)
            samples.append(
                any(
                    run_id == handle.result_run_id
                    for run_id, _ in _PENDING_STREAM_CALLS.keys()
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
    assert result.stop == "Match"
    # Heartbeats happened, and the one Local Activity ran through them uncancelled.
    assert counts["WORKFLOW_TASK_COMPLETED"] >= 3
    assert reads == 2
    assert store.cancelled_reads == 0
    assert all(samples)
    assert not any(
        run_id == handle.result_run_id for run_id, _ in _PENDING_STREAM_CALLS.keys()
    )


async def _record_sizes(store: Any, stream_id: str) -> list[int]:
    response = await store.read_records(
        ReadRecordsRequest(ref=StreamRefMessage(id=stream_id, store=store.name))
    )
    return [record.ByteSize() for record in response.records]


async def test_take_max_bytes_boundary(client: Client):
    stream_client, store, stream_id = await _new_stream(client)
    tokens = _tokens(5)
    await _append(stream_client, stream_id, *tokens)
    sizes = await _record_sizes(store, stream_id)
    # The third Record passes the cap by one byte.
    _, result = await _run_workflow(
        stream_client,
        CallWorkflow,
        CallArgs(stream_id, until_kind=None, max_bytes=sum(sizes[:3]) - 1),
    )
    assert result.texts == _texts(tokens[:2])
    assert result.stop == "MaxBytes"


async def _run_flowing_take(
    client: Client, *, seconds: float = 2.0
) -> tuple[WorkflowHistory, float]:
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [*_tokens(39), _done()]
    async with new_worker(stream_client, CallWorkflow) as worker:
        handle = await _start_call(
            stream_client,
            CallArgs(stream_id, timeout_seconds=30, idle_timeout_seconds=5),
            worker.task_queue,
        )
        appended = await _append_over(stream_client, stream_id, tokens, seconds)
        result = await handle.result()
        latency = time.monotonic() - appended
    assert result.texts == _texts(tokens)
    return await handle.fetch_history(), latency


async def test_measure_flowing_take(client: Client):
    history, _ = await _run_flowing_take(client)
    payload = _marker_payload(history)
    print(f"\nA. 40 Records over 2 s, History events: {_event_counts(history)}")
    print(f"A. Result payload: {payload.ByteSize()} bytes")


async def test_measure_take_of_forty(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    await _append(stream_client, stream_id, *_tokens(40))
    await _close(stream_client, stream_id)
    handle, result = await _run_workflow(
        stream_client, CallWorkflow, CallArgs(stream_id, until_kind=None)
    )
    assert (result.count, result.stop) == (40, "Done")
    payload = _marker_payload(await handle.fetch_history())
    header = Payloads.FromString(payload.data).payloads[0].data.decode()
    print(f"\nResult of a take of 40 small Records: {payload.ByteSize()} bytes")
    print(f"Its header: {header}")


@workflow.defn
class KeepHitsWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> int:
        read = await _stream(stream_id).filter(
            lambda record: record.body.kind == "hit",
            timedelta(seconds=10),
            idle_timeout=timedelta(0),
        )
        return len(read.records)


async def test_measure_filter_that_keeps_five(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [
        Token("hit" if index % 8 == 0 else "text", f"{_BODY_MARK}-{index}")
        for index in range(40)
    ]
    await _append(stream_client, stream_id, *tokens)
    await _close(stream_client, stream_id)
    handle, kept = await _run_workflow(stream_client, KeepHitsWorkflow, stream_id)
    assert kept == 5
    payload = _marker_payload(await handle.fetch_history())
    print(f"\nResult of a filter that keeps 5 of 40: {payload.ByteSize()} bytes")


async def test_measure_idle_wait(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    async with new_worker(stream_client, CallWorkflow) as worker:
        handle = await _start_call(
            stream_client,
            CallArgs(stream_id, timeout_seconds=30, idle_timeout_seconds=30),
            worker.task_queue,
            task_timeout=timedelta(seconds=2),
        )
        await asyncio.sleep(6)
        await _append(stream_client, stream_id, _done())
        await handle.result()
    counts = _event_counts(await handle.fetch_history())
    print(f"\nA wait of 6 s with idle_timeout=30 s, History events: {counts}")


async def test_measure_match_latency(client: Client):
    latencies = []
    for _ in range(5):
        _, latency = await _run_flowing_take(client)
        latencies.append(latency)
    print(
        "\nAppend of the match to the end of the Workflow: "
        + ", ".join(f"{latency * 1000:.0f} ms" for latency in latencies)
    )


async def _signal_wait(client: Client) -> float:
    stream_client, _, stream_id = await _new_stream(client)
    async with new_worker(stream_client, TakeUntilWantedWorkflow) as worker:
        handle = await stream_client.start_workflow(
            TakeUntilWantedWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await asyncio.sleep(1)
        await handle.signal(TakeUntilWantedWorkflow.want, "never")

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
        "\nSignal to the next Workflow Task Started while a call waits: "
        + ", ".join(f"{wait:.2f} s" for wait in waits)
    )


async def test_measure_flowing_fold(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    tokens = [*_tokens(39), _done()]
    async with new_worker(stream_client, CallWorkflow) as worker:
        handle = await _start_call(
            stream_client,
            CallArgs(
                stream_id, call="reduce", timeout_seconds=30, idle_timeout_seconds=5
            ),
            worker.task_queue,
        )
        await _append_over(stream_client, stream_id, tokens, 2.0)
        result = await handle.result()
    assert result.count == 40
    history = await handle.fetch_history()
    payload = _marker_payload(history)
    print(
        f"\nD. A fold over 40 Records over 2 s, History events: {_event_counts(history)}"
    )
    print(f"D. Result payload: {payload.ByteSize()} bytes")


async def test_fold_appends_each_record_to_another_stream(client: Client):
    store = RecordingStreamStore(MemoryStreamStore())
    stream_client, _, source_id = await _new_stream(client, store)
    target_id = f"stream-{uuid.uuid4()}"
    await stream_client.create_stream(id=target_id, body_type=str)
    tokens = _tokens(40)
    await _append(stream_client, source_id, *tokens)
    await _close(stream_client, source_id)
    handle, result = await _run_workflow(
        stream_client, FoldThatAppendsWorkflow, source_id, target_id
    )
    assert result == "40 Done"
    target = stream_client.get_stream(StreamRef(id=target_id), body_type=str)
    records = [record async for record in target.iter_read_items()]
    assert [record.body for record in records] == [
        text.upper() for text in _texts(tokens)
    ]
    run_id = handle.result_run_id
    producer = records[0].producer
    assert producer is not None and isinstance(producer.kind, ProducerKindLocalActivity)
    assert producer.kind.value.run_id == run_id
    history = await handle.fetch_history()
    counts = _event_counts(history)
    # One marker for the fold. The appends went to the Store from its Local Activity.
    assert _markers(history) == ["core_local_activity"]
    assert "TIMER_STARTED" not in counts
    print(f"\nE. A fold that appends 40 Records to another Stream: {counts}")
    operations = len(store.operations)
    await Replayer(workflows=[FoldThatAppendsWorkflow]).replay_workflow(history)
    await Replayer(
        workflows=[FoldThatAppendsWorkflow], stream_config=StreamConfig(stores=[store])
    ).replay_workflow(history)
    assert len(store.operations) == operations
    assert len([record async for record in target.iter_read_items()]) == 40


async def test_failed_append_in_a_function_fails_the_call(client: Client):
    stream_client, _, source_id = await _new_stream(client)
    await _append(stream_client, source_id, *_tokens(2))
    _, result = await _run_workflow(
        stream_client,
        FoldThatAppendsWorkflow,
        source_id,
        f"stream-{uuid.uuid4()}",
    )
    assert result == "STREAM_ERROR_REASON_NOT_FOUND"
