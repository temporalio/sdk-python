"""Tests of the take, filter and reduce calls of client and Activity code."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from temporalio import activity, workflow
from temporalio.client import (
    Client,
    StreamReadStop,
    filter_stream,
    reduce_stream,
    take_stream,
)
from temporalio.contrib.streams import BufferedAppender
from temporalio.exceptions import ApplicationError
from temporalio.nexus.system.stream_service import StreamRecord, StreamRef
from temporalio.testing import MemoryStreamStore
from tests.helpers import new_worker
from tests.helpers.streams import RecordingStreamStore
from tests.nexus.test_stream_read_workflow import (
    _STOPS,
    CallArgs,
    CallResult,
    Token,
    _append,
    _call,
    _check_stop,
    _close,
    _new_stream,
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
        read = await filter_stream(
            stream,
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
        await take_stream(stream, timedelta(seconds=5), awaiting)
    with pytest.raises(ApplicationError) as err:
        await filter_stream(
            stream, lambda record: awaiting(record), timedelta(seconds=5)
        )
    assert err.value.non_retryable
    assert err.value.message.startswith("The predicate of a Stream call returned an")

    def raises(value: int, record: StreamRecord[Token]) -> int:
        raise ValueError(f"bad fold after {value} at {record.offset.hex()}")

    # In a client, the error of the caller's function reaches it as it is.
    with pytest.raises(ValueError, match="bad fold"):
        await reduce_stream(stream, raises, 0, timedelta(seconds=5))


async def test_client_result_over_the_limit(client: Client):
    stream_client, _, stream_id = await _new_stream(client)
    for _ in range(6):
        await _append(stream_client, stream_id, *[Token("text", "x" * 20_000)] * 10)
    stream = _client_stream(stream_client, stream_id)
    with pytest.raises(ApplicationError) as err:
        await take_stream(
            stream, timedelta(seconds=5), idle_timeout=timedelta(0), max_bytes=1 << 22
        )
    assert err.value.type == "StreamResultTooLarge"
    assert "over the limit of 1048576 bytes" in err.value.message
    # The default cap of 256 KiB keeps a call under the limit.
    read = await take_stream(stream, timedelta(seconds=5), idle_timeout=timedelta(0))
    assert read.stop is StreamReadStop.MaxBytes


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

    result = await reduce_stream(
        _client_stream(stream_client, source_id),
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
