"""Tests of the take, filter and reduce calls of client and Activity code."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from temporalio import activity, workflow
from temporalio.client import (
    Client,
    Interceptor,
    NexusOperationHandle,
    OutboundInterceptor,
    StartNexusOperationInput,
)
from temporalio.contrib.streams import BufferedAppender
from temporalio.exceptions import ApplicationError
from temporalio.nexus.system.stream_service import (
    ReadRecordsRequest,
    ReadRecordsResponse,
    StreamReadStop,
    StreamRecord,
    StreamRef,
)
from temporalio.nexus.system.stream_service.client import Stream as ClientStream
from temporalio.nexus.system.stream_service.workflow._resources.stream import (
    Stream as WorkflowStream,
)
from temporalio.testing import MemoryStreamStore
from tests.helpers import new_worker
from tests.helpers.streams import RecordingStreamStore
from tests.nexus.test_stream_read_workflow import (
    _STOPS,
    CallArgs,
    CallResult,
    CallWorkflow,
    OverLimitWorkflow,
    Token,
    _append,
    _call,
    _check_stop,
    _close,
    _new_stream,
    _offsets,
    _record_sizes,
    _run_workflow,
    _tokens,
    _with_store,
)


def _client_stream(stream_client: Client, stream_id: str) -> Any:
    return stream_client.get_stream(StreamRef(id=stream_id), body_type=Token)


@activity.defn
async def call_in_activity(args: CallArgs) -> CallResult:
    stream = activity.client().get_stream(StreamRef(id=args.stream_id), body_type=Token)
    return await _call(args, stream)


@activity.defn
async def filter_loop_in_activity(stream_id: str) -> list[str]:
    stream = activity.client().get_stream(StreamRef(id=stream_id), body_type=Token)
    return await _filter_loop(stream)


async def _filter_loop(stream: Any) -> list[str]:
    found: list[str] = []
    offset: bytes | None = None
    while True:
        read = await stream.filter(
            lambda record: record.body.kind == "hit",
            timedelta(seconds=10),
            from_offset=offset,
            idle_timeout=timedelta(0),
            max_records=1,
        )
        found.extend(record.body.text for record in read.records)
        offset = read.last_offset
        if read.stop in (StreamReadStop.Done, StreamReadStop.Idle):
            return found


@workflow.defn
class ActivityCallWorkflow:
    @workflow.run
    async def run(self, args: CallArgs) -> CallResult:
        return await workflow.execute_activity(
            call_in_activity, args, start_to_close_timeout=timedelta(seconds=30)
        )


@workflow.defn
class ActivityLoopWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[str]:
        return await workflow.execute_activity(
            filter_loop_in_activity,
            stream_id,
            start_to_close_timeout=timedelta(seconds=30),
        )


async def _loop_stream(stream_client: Client) -> tuple[str, list[str]]:
    stream_id = f"stream-{uuid.uuid4()}"
    await stream_client.create_stream(id=stream_id, body_type=Token)
    tokens = [
        Token("hit" if index % 4 == 1 else "text", f"record-{index}")
        for index in range(20)
    ]
    await _append(stream_client, stream_id, *tokens)
    await _close(stream_client, stream_id)
    return stream_id, [token.text for token in tokens if token.kind == "hit"]


@pytest.mark.parametrize("call", ["take", "filter", "reduce"])
@pytest.mark.parametrize("stop", _STOPS)
async def test_client_call_stop_and_last_offset(client: Client, call: str, stop: str):
    stream_client = _with_store(client, MemoryStreamStore())

    async def run(stream_id: str, options: dict[str, Any]) -> CallResult:
        args = CallArgs(stream_id, call=call, **options)
        result = await _call(args, _client_stream(stream_client, stream_id))
        assert result.typed
        return result

    await _check_stop(stream_client, call, stop, run)


async def test_client_filter_loop_reads_to_the_end(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id, hits = await _loop_stream(stream_client)
    assert await _filter_loop(_client_stream(stream_client, stream_id)) == hits


@pytest.mark.parametrize("stop", _STOPS)
async def test_activity_call_stop_and_last_offset(client: Client, stop: str):
    stream_client = _with_store(client, MemoryStreamStore())
    # Each stop once, and the three calls in turn.
    call = ["take", "filter", "reduce"][_STOPS.index(stop) % 3]
    async with new_worker(
        stream_client, ActivityCallWorkflow, activities=[call_in_activity]
    ) as worker:

        async def run(stream_id: str, options: dict[str, Any]) -> CallResult:
            return await stream_client.execute_workflow(
                ActivityCallWorkflow.run,
                CallArgs(stream_id, call=call, **options),
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

        await _check_stop(stream_client, call, stop, run)


async def test_activity_filter_loop_reads_to_the_end(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id, hits = await _loop_stream(stream_client)
    async with new_worker(
        stream_client, ActivityLoopWorkflow, activities=[filter_loop_in_activity]
    ) as worker:
        found = await stream_client.execute_workflow(
            ActivityLoopWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert found == hits


async def test_client_functions_must_be_plain(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    await _append(stream_client, stream_id, *_tokens(1))
    stream = _client_stream(stream_client, stream_id)

    async def coroutine_until(record: StreamRecord[Token]) -> bool:
        del record
        return True

    # The type checker refuses both functions, and the call refuses them at run time.
    awaiting: Any = coroutine_until

    with pytest.raises(TypeError, match="until must be a plain function"):
        await stream.take(timedelta(seconds=5), awaiting)
    with pytest.raises(ApplicationError) as err:
        await stream.filter(lambda record: awaiting(record), timedelta(seconds=5))
    assert err.value.non_retryable
    assert err.value.message.startswith("The predicate of a Stream call returned an")

    def raises(value: int, record: StreamRecord[Token]) -> int:
        raise ValueError(f"bad fold after {value} at {record.offset.hex()}")

    # In a client, the error of the caller's function reaches it as it is.
    with pytest.raises(ValueError, match="bad fold"):
        await stream.reduce(raises, 0, timedelta(seconds=5))


async def test_client_result_has_no_limit(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    for _ in range(6):
        await _append(stream_client, stream_id, *[Token("text", "x" * 20_000)] * 10)
    await _close(stream_client, stream_id)
    stream = _client_stream(stream_client, stream_id)
    # Over 1 MiB of Records, which a client gets since its result doesn't go into History.
    read = await stream.take(
        timedelta(seconds=5), idle_timeout=timedelta(0), max_bytes=1 << 22
    )
    assert len(read.records) == 60
    assert read.stop is StreamReadStop.Done
    _, message = await _run_workflow(
        stream_client, OverLimitWorkflow, stream_id, "take"
    )
    assert message.startswith("StreamResultTooLarge True: ")


def test_stream_methods():
    for stream_class in (ClientStream, WorkflowStream):
        for name in ("take", "filter", "reduce"):
            assert callable(getattr(stream_class, name, None)), (stream_class, name)
    assert hasattr(ClientStream, "read")
    assert not hasattr(WorkflowStream, "read")


class _PageCounter(Interceptor):
    """Counts the ReadRecords operations that the client starts."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return _PageCounterOutbound(self, next)


class _PageCounterOutbound(OutboundInterceptor):
    def __init__(self, counter: _PageCounter, next: OutboundInterceptor) -> None:
        super().__init__(next)
        self._counter = counter

    async def start_nexus_operation(
        self, input: StartNexusOperationInput
    ) -> NexusOperationHandle[Any]:
        if input.operation == "ReadRecords":
            self._counter.reads += 1
        return await super().start_nexus_operation(input)


def _with_interceptor(stream_client: Client, interceptor: Interceptor) -> Client:
    config = stream_client.config()
    config["interceptors"] = [*config["interceptors"], interceptor]
    return Client(**config)


async def test_client_interceptor_sees_each_page(client: Client):
    store = RecordingStreamStore(MemoryStreamStore())
    stream_client, _, stream_id = await _new_stream(client, store)
    for start in range(0, 2500, 1000):
        await _append(stream_client, stream_id, *_tokens(1000, prefix=f"p{start}-"))
    counter = _PageCounter()
    read = await _client_stream(
        _with_interceptor(stream_client, counter), stream_id
    ).take(
        timedelta(seconds=10),
        idle_timeout=timedelta(0),
        max_records=2500,
        max_bytes=1 << 30,
    )
    assert len(read.records) == 2500
    assert read.stop is StreamReadStop.MaxRecords
    # A page holds at most 1,000 Records, so the take reads three pages.
    assert counter.reads == store.operations.count("ReadRecords") == 3


@pytest.mark.parametrize("call", ["take", "filter", "reduce"])
async def test_max_bytes_stops_at_the_same_offset_everywhere(client: Client, call: str):
    store = MemoryStreamStore()
    stream_client, _, stream_id = await _new_stream(client, store)
    # Every other Record is one that the filter drops, so it counts only the kept ones.
    tokens = [
        Token("skip" if index % 2 else "text", f"record-{index}") for index in range(12)
    ]
    await _append(stream_client, stream_id, *tokens)
    sizes = await _record_sizes(store, stream_id)
    args = CallArgs(
        stream_id,
        call=call,
        until_kind=None,
        idle_timeout_seconds=0,
        max_bytes=sum(sizes[:3]) - 1,
    )
    in_client = await _call(args, _client_stream(stream_client, stream_id))
    async with new_worker(
        stream_client,
        CallWorkflow,
        ActivityCallWorkflow,
        activities=[call_in_activity],
    ) as worker:
        in_activity = await stream_client.execute_workflow(
            ActivityCallWorkflow.run,
            args,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        in_workflow = await stream_client.execute_workflow(
            CallWorkflow.run,
            args,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert in_client.stop == "MaxBytes"
    assert in_client == in_activity == in_workflow
    assert in_client.count == 2
    # The dropped Records count no bytes, so the filter scans past two of them.
    offsets = await _offsets(stream_client, stream_id)
    assert in_client.last_offset == offsets[3 if call == "filter" else 1]


class _ReadAnswerer(Interceptor):
    """Answers each ReadRecords operation itself, with no call of the Store."""

    def __init__(self, records: list[StreamRecord[Token]]) -> None:
        super().__init__()
        self.records = records

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return _ReadAnswererOutbound(self, next)


class _AnsweredHandle:
    def __init__(self, response: ReadRecordsResponse[Token]) -> None:
        self._response = response

    async def result(self) -> ReadRecordsResponse[Token]:
        return self._response


class _ReadAnswererOutbound(OutboundInterceptor):
    def __init__(self, answerer: _ReadAnswerer, next: OutboundInterceptor) -> None:
        super().__init__(next)
        self._answerer = answerer

    async def start_nexus_operation(
        self, input: StartNexusOperationInput
    ) -> NexusOperationHandle[Any]:
        if input.operation != "ReadRecords":
            return await super().start_nexus_operation(input)
        request = input.arg
        assert isinstance(request, ReadRecordsRequest)
        records = [
            record
            for record in self._answerer.records
            if request.from_offset is None or record.offset > request.from_offset
        ]
        response = ReadRecordsResponse(
            records=records,
            last_offset=records[-1].offset if records else request.from_offset,
            done=True,
        )
        # The generated `read` only awaits `result()` of the handle.
        handle: Any = _AnsweredHandle(response)
        return handle


async def test_interceptor_that_answers_a_read_counts_no_bytes(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    records = [
        StreamRecord(body=Token("text", "x" * 1000), offset=bytes([index]))
        for index in range(1, 6)
    ]
    answered = _with_interceptor(stream_client, _ReadAnswerer(records))
    # No Store call measured these Records, so a cap of one byte doesn't stop the take.
    read = await _client_stream(answered, stream_id).take(
        timedelta(seconds=5), idle_timeout=timedelta(0), max_bytes=1
    )
    assert [record.offset for record in read.records] == [
        record.offset for record in records
    ]
    assert read.stop is StreamReadStop.Done
    assert read.last_offset == bytes([5])


async def test_client_appender_in_a_function_writes_before_the_call_returns(
    client: Client,
):
    store = RecordingStreamStore(MemoryStreamStore())
    stream_client, _, source_id = await _new_stream(client, store)
    target = await stream_client.create_stream(
        id=f"stream-{uuid.uuid4()}", body_type=str
    )
    await _append(stream_client, source_id, *_tokens(5))
    # A long delay, so only the end of the call writes the buffered Records.
    appender = BufferedAppender(target, max_delay=timedelta(hours=1))

    def append_upper(value: int, record: StreamRecord[Token]) -> int:
        appender.append(record.body.text.upper())
        return value + 1

    result = await _client_stream(stream_client, source_id).reduce(
        append_upper,
        0,
        timedelta(seconds=5),
        idle_timeout=timedelta(0),
    )
    assert result.count == 5
    assert store.operations.count("AppendRecords") == 2
    page = await target.read()
    assert [record.body for record in page.records] == [
        token.text.upper() for token in _tokens(5)
    ]
