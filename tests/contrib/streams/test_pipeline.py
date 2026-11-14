"""Tests of the pipeline of `temporalio.contrib.streams`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from google.protobuf.message import Message

import temporalio.api.streamservice.v1
from temporalio import workflow
from temporalio.client import Client, StreamConfig, StreamStoreCallOptions
from temporalio.common import RetryPolicy
from temporalio.contrib.streams import Pipeline, pipeline
from temporalio.nexus.system.stream_service import (
    StreamRead,
    StreamReadStop,
    StreamRecord,
    StreamReduce,
    StreamRef,
)
from temporalio.testing import MemoryStreamStore
from temporalio.worker import Replayer
from tests.helpers import new_worker
from tests.helpers.streams import RecordingStreamStore
from tests.nexus.test_stream_read_workflow import (
    Token,
    _append,
    _close,
    _markers,
    _offsets,
    _with_store,
)

_QUICK = timedelta(seconds=10)
_NOW = timedelta(0)

# Two lines of text, with Records of another kind between them.
_TOKENS = [
    Token("text", "a"),
    Token("skip", "x"),
    Token("text", "b\n"),
    Token("text", "c"),
    Token("skip", "y"),
    Token("text", "d\n"),
    Token("text", "e"),
]


def _is_text(record: StreamRecord[Token]) -> bool:
    return record.body.kind == "text"


def _text(record: StreamRecord[Token]) -> str:
    return record.body.text


def _ends_line(text: str) -> bool:
    return text.endswith("\n")


def _join(joined: str, text: str) -> str:
    return joined + text


def _record_ends_line(record: StreamRecord[Token]) -> bool:
    return record.body.text.endswith("\n")


@dataclass
class ChainResult:
    name: str
    values: list[str]
    count: int
    stop: str
    last_offset: str | None
    done: bool


def _of_read(name: str, read: StreamRead[Token]) -> ChainResult:
    return ChainResult(
        name,
        [record.body.text for record in read.records],
        len(read.records),
        read.stop.name,
        read.last_offset.hex() if read.last_offset else None,
        read.done,
    )


def _of_reduce(name: str, result: StreamReduce[Any], values: list[str]) -> ChainResult:
    return ChainResult(
        name,
        values,
        result.count,
        result.stop.name,
        result.last_offset.hex() if result.last_offset else None,
        result.done,
    )


async def _run_chains(stream: Any) -> list[ChainResult]:
    """Run each step and each end once over the Stream of `_TOKENS`."""
    start = pipeline(stream)
    texts = start.filter(_is_text)
    results = [
        _of_read("filter records", await texts.records(_QUICK, idle_timeout=_NOW)),
        _of_read(
            "take_until records",
            await texts.take_until(_record_ends_line).records(
                _QUICK, idle_timeout=_NOW
            ),
        ),
        _of_read(
            "limit records", await texts.limit(3).records(_QUICK, idle_timeout=_NOW)
        ),
        _of_read("all records", await start.records(_QUICK, idle_timeout=_NOW)),
    ]
    line = (
        await texts.map(_text)
        .take_until(_ends_line)
        .reduce(_join, "", _QUICK, idle_timeout=_NOW)
    )
    results.append(_of_reduce("reduce", line, [line.value]))
    collected = await texts.map(_text).limit(4).collect(_QUICK, idle_timeout=_NOW)
    results.append(_of_reduce("collect", collected, collected.value))
    records = await texts.limit(2).collect(_QUICK, idle_timeout=_NOW)
    results.append(
        _of_reduce(
            "collect records", records, [record.body.text for record in records.value]
        )
    )
    count = await texts.count(_QUICK, idle_timeout=_NOW)
    results.append(_of_reduce("count", count, [str(count.value)]))
    early = await start.limit(3).filter(_is_text).count(_QUICK, idle_timeout=_NOW)
    results.append(_of_reduce("limit then filter", early, [str(early.value)]))
    return results


def _expected(offsets: list[str]) -> list[ChainResult]:
    return [
        ChainResult("filter records", ["a", "b\n", "c", "d\n", "e"], 5, "Done", offsets[6], True),
        ChainResult("take_until records", ["a", "b\n"], 2, "Match", offsets[2], False),
        ChainResult("limit records", ["a", "b\n", "c"], 3, "MaxRecords", offsets[3], False),
        ChainResult("all records", [token.text for token in _TOKENS], 7, "Done", offsets[6], True),
        ChainResult("reduce", ["ab\n"], 2, "Match", offsets[2], False),
        ChainResult("collect", ["a", "b\n", "c", "d\n"], 4, "Match", offsets[5], False),
        ChainResult("collect records", ["a", "b\n"], 2, "MaxRecords", offsets[2], False),
        ChainResult("count", ["5"], 5, "Done", offsets[6], True),
        # The limit counts every Record, and the filter after it drops the skipped one.
        ChainResult("limit then filter", ["2"], 2, "Match", offsets[2], False),
    ]  # fmt: skip


def _workflow_stream(stream_id: str) -> Any:
    return workflow.get_stream(StreamRef(id=stream_id), body_type=Token)


@workflow.defn
class ChainsWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[ChainResult]:
        return await _run_chains(_workflow_stream(stream_id))


@workflow.defn
class OneChainWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        result = (
            await pipeline(_workflow_stream(stream_id))
            .filter(_is_text)
            .map(_text)
            .take_until(_ends_line)
            .reduce(_join, "", _QUICK, idle_timeout=_NOW)
        )
        return result.value


async def _hits_one_by_one(stream: Any) -> list[str]:
    found: list[str] = []
    offset: bytes | None = None
    while True:
        read = (
            await pipeline(stream)
            .filter(_is_text)
            .limit(1)
            .records(_QUICK, from_offset=offset, idle_timeout=_NOW)
        )
        found.extend(record.body.text for record in read.records)
        offset = read.last_offset
        if read.stop in (StreamReadStop.Done, StreamReadStop.Idle):
            return found


async def _lines(stream: Any) -> list[str]:
    lines: list[str] = []
    offset: bytes | None = None
    while True:
        line = (
            await pipeline(stream)
            .filter(_is_text)
            .map(_text)
            .take_until(_ends_line)
            .reduce(_join, "", _QUICK, from_offset=offset, idle_timeout=_NOW)
        )
        if line.count:
            lines.append(line.value)
        offset = line.last_offset
        if line.stop in (StreamReadStop.Done, StreamReadStop.Idle):
            return lines


@workflow.defn
class LoopsWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> tuple[list[str], list[str]]:
        stream = _workflow_stream(stream_id)
        return await _hits_one_by_one(stream), await _lines(stream)


@workflow.defn
class LimitAndUntilWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> list[ChainResult]:
        texts = pipeline(_workflow_stream(stream_id)).filter(_is_text).map(_text)
        limited = await texts.limit(4).collect(_QUICK, idle_timeout=_NOW)
        line = await texts.take_until(_ends_line).reduce(
            _join, "", _QUICK, idle_timeout=_NOW
        )
        return [
            _of_reduce("limit", limited, limited.value),
            _of_reduce("take_until", line, [line.value]),
        ]


class SmallPagesStore(RecordingStreamStore):
    """Gives two Records a page, and fails one read of the scan when it is armed."""

    def __init__(self, *, fail_read: int | None) -> None:
        super().__init__(MemoryStreamStore())
        self.fail_read = fail_read
        self.armed = False
        self.reads = 0

    async def before_call(self, operation: str, request: Message) -> None:
        if not isinstance(request, temporalio.api.streamservice.v1.ReadRecordsRequest):
            return
        request.max_records = min(request.max_records or 2, 2)
        if not self.armed:
            return
        self.reads += 1
        if self.reads == self.fail_read:
            raise RuntimeError("The Store failed one read")


def _retrying_client(client: Client, store: SmallPagesStore) -> Client:
    config = client.config()
    config["stream_config"] = StreamConfig(
        stores=[store],
        workflow_call_options=StreamStoreCallOptions(
            retry_policy=RetryPolicy(
                maximum_attempts=3, initial_interval=timedelta(milliseconds=10)
            )
        ),
    )
    return Client(**config)


async def _stream_of_tokens(
    client: Client, stream_client: Client | None = None
) -> tuple[Client, str, list[str]]:
    stream_client = stream_client or _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    await stream_client.create_stream(id=stream_id, body_type=Token)
    await _append(stream_client, stream_id, *_TOKENS)
    await _close(stream_client, stream_id)
    return stream_client, stream_id, await _offsets(stream_client, stream_id)


async def _execute(stream_client: Client, workflow_class: type, *args: Any) -> Any:
    async with new_worker(stream_client, workflow_class) as worker:
        handle = await stream_client.start_workflow(
            getattr(workflow_class, "run"),
            args=list(args),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        return handle, await handle.result()


async def test_each_step_and_end_in_workflow_code(client: Client):
    stream_client, stream_id, offsets = await _stream_of_tokens(client)
    _, results = await _execute(stream_client, ChainsWorkflow, stream_id)
    assert results == _expected(offsets)


async def test_each_step_and_end_from_a_client(client: Client):
    stream_client, stream_id, offsets = await _stream_of_tokens(client)
    stream = stream_client.get_stream(StreamRef(id=stream_id), body_type=Token)
    assert await _run_chains(stream) == _expected(offsets)


async def test_a_chain_is_one_marker(client: Client):
    stream_client, stream_id, _ = await _stream_of_tokens(client)
    handle, line = await _execute(stream_client, OneChainWorkflow, stream_id)
    assert line == "ab\n"
    assert len(_markers(await handle.fetch_history())) == 1


async def test_records_refuses_a_chain_that_does_not_fit(client: Client):
    stream = client.get_stream(StreamRef(id="unused"), body_type=Token)
    texts = pipeline(stream).filter(_is_text)
    cases: list[tuple[Pipeline[Any], str]] = [
        (texts.map(_text), "map"),
        (texts.take_until(_is_text).take_until(_is_text), "take_until, the second one"),
        (texts.limit(1).filter(_is_text), "limit, which isn't the last step"),
    ]
    for chain, step in cases:
        with pytest.raises(TypeError) as err:
            chain.records(_QUICK)
        assert str(err.value) == (
            f"records() can't run this chain, since its step {step} doesn't fit. It "
            "runs only filter steps, one take_until and a limit as the last step. End "
            "the chain with collect() or reduce()."
        )


async def test_loops_read_every_record_once(client: Client):
    stream_client, stream_id, _ = await _stream_of_tokens(client)
    _, (hits, lines) = await _execute(stream_client, LoopsWorkflow, stream_id)
    assert hits == ["a", "b\n", "c", "d\n", "e"]
    assert lines == ["ab\n", "cd\n", "e"]
    stream = stream_client.get_stream(StreamRef(id=stream_id), body_type=Token)
    assert await _hits_one_by_one(stream) == hits
    assert await _lines(stream) == lines


async def test_failed_local_activity_attempt_gives_the_clean_result(client: Client):
    clean_store = SmallPagesStore(fail_read=None)
    clean_client, clean_id, clean_offsets = await _stream_of_tokens(
        client, _retrying_client(client, clean_store)
    )
    _, clean = await _execute(clean_client, LimitAndUntilWorkflow, clean_id)

    failing_store = SmallPagesStore(fail_read=2)
    failing_client, failing_id, failing_offsets = await _stream_of_tokens(
        client, _retrying_client(client, failing_store)
    )
    failing_store.armed = True
    _, after_failure = await _execute(failing_client, LimitAndUntilWorkflow, failing_id)

    # The second read failed after the first page went through the steps.
    assert failing_store.reads > 2
    # The two Streams are different, so the results name a Record by its place.
    for results, offsets in ((clean, clean_offsets), (after_failure, failing_offsets)):
        for result in results:
            result.last_offset = str(offsets.index(result.last_offset or ""))
    assert after_failure == clean
    assert clean[0].values == ["a", "b\n", "c", "d\n"]
    assert clean[1].values == ["ab\n"]


async def test_replayer_replays_each_end(client: Client):
    stream_client, stream_id, offsets = await _stream_of_tokens(client)
    handle, results = await _execute(stream_client, ChainsWorkflow, stream_id)
    assert results == _expected(offsets)
    await Replayer(workflows=[ChainsWorkflow]).replay_workflow(
        await handle.fetch_history()
    )


async def test_typed_ends(client: Client):
    stream_client, stream_id, _ = await _stream_of_tokens(client)
    stream = stream_client.get_stream(StreamRef(id=stream_id), body_type=Token)
    texts: Pipeline[str] = pipeline(stream).filter(_is_text).map(_text)
    read: StreamRead[Token] = await pipeline(stream).limit(1).records(_QUICK)
    joined: StreamReduce[str] = await texts.reduce(_join, "", _QUICK, idle_timeout=_NOW)
    items: StreamReduce[list[str]] = await texts.collect(_QUICK, idle_timeout=_NOW)
    count: StreamReduce[int] = await texts.count(_QUICK, idle_timeout=_NOW)
    assert read.records[0].body == _TOKENS[0]
    assert joined.value == "ab\ncd\ne"
    assert items.value == ["a", "b\n", "c", "d\n", "e"]
    assert count.value == 5
