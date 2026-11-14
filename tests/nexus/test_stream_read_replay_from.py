"""Tests of `StreamConfig.workflow_read_replay_from` for the take and filter of Workflow code."""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest
from google.protobuf.message import Message

from temporalio import workflow
from temporalio.api.common.v1 import Payload, Payloads
from temporalio.api.stream.v1 import StreamRef as StreamRefMessage
from temporalio.api.streamservice.v1 import ReadRecordsRequest
from temporalio.client import (
    Client,
    StreamConfig,
    StreamReplaySource,
    WorkflowHistory,
)
from temporalio.converter import DataConverter
from temporalio.testing import MemoryStreamStore
from temporalio.worker import Replayer
from tests.nexus.test_stream_read_workflow import (
    _BODY_MARK,
    CallArgs,
    CallResult,
    CallWorkflow,
    KeepHitsWorkflow,
    Token,
    XorCodec,
    _append,
    _call,
    _close,
    _done,
    _keep,
    _marker_payload,
    _markers,
    _texts,
    _tokens,
)

with workflow.unsafe.imports_passed_through():
    from tests.helpers import assert_eventually, new_worker
    from tests.helpers.streams import (
        DictStreamStore,
        RecordingStreamStore,
        StreamReadState,
    )

_HISTORY = StreamReplaySource.HISTORY
_STORE = StreamReplaySource.STORE
_READ_CALLS = pytest.mark.parametrize("call", ["take", "filter"])

# The Records of a call. The filter drops the two of the kind "skip".
_CALL_TOKENS = [
    Token("text", f"{_BODY_MARK}-t0"),
    Token("skip", f"{_BODY_MARK}-s1"),
    Token("text", f"{_BODY_MARK}-t2"),
    Token("skip", f"{_BODY_MARK}-s3"),
    Token("text", f"{_BODY_MARK}-t4"),
    _done(),
]


class RequestsStore(RecordingStreamStore):
    """A Store that keeps each read request, to show what a replay reads."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner)
        self.reads: list[ReadRecordsRequest] = []

    async def before_call(self, operation: str, request: Message) -> None:
        if isinstance(request, ReadRecordsRequest):
            self.reads.append(request)


@workflow.defn
class QuietCallWorkflow:
    """Runs the call of ``args`` and gives back no text, so no body is in its result."""

    @workflow.run
    async def run(self, args: CallArgs) -> int:
        return _keep(await _call(args)).count


def _config(store: Any, replay_from: StreamReplaySource) -> StreamConfig:
    return StreamConfig(stores=[store], workflow_read_replay_from=replay_from)


def _client(
    client: Client,
    store: Any,
    replay_from: StreamReplaySource,
    data_converter: DataConverter | None = None,
) -> Client:
    config = client.config()
    config["stream_config"] = _config(store, replay_from)
    if data_converter is not None:
        config["data_converter"] = data_converter
    return Client(**config)


async def _stream_of(client: Client, *tokens: Token, closed: bool = False) -> str:
    stream_id = f"stream-{uuid.uuid4()}"
    await client.create_stream(id=stream_id, body_type=Token)
    for start in range(0, len(tokens), 10):
        await _append(client, stream_id, *tokens[start : start + 10])
    if closed:
        await _close(client, stream_id)
    return stream_id


async def _run(
    client: Client, workflow_class: type, *args: Any
) -> tuple[WorkflowHistory, Any]:
    async with new_worker(client, workflow_class) as worker:
        handle = await client.start_workflow(
            getattr(workflow_class, "run"),
            args=list(args),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await handle.result()
        history = await handle.fetch_history()
    return history, result


async def _plain_marker(
    history: WorkflowHistory,
    data_converter: DataConverter | None = None,
    index: int = 0,
) -> Payload:
    payload = _marker_payload(history, index)
    if data_converter is not None and data_converter.payload_codec is not None:
        [payload] = await data_converter.payload_codec.decode([payload])
    return payload


def _replays_from_store(payload: Payload) -> bool:
    return payload.metadata.get("temporal-stream-replay-from") == b"store"


def _no_body_in(history: WorkflowHistory) -> bool:
    return _BODY_MARK not in history.to_json() and not any(
        _BODY_MARK.encode() in event.SerializeToString() for event in history.events
    )


async def _store_records(store: Any, stream_id: str) -> list[Any]:
    response = await store.inner.read_records(
        ReadRecordsRequest(ref=StreamRefMessage(id=stream_id, store=store.name))
    )
    return list(response.records)


def _run_result(history: WorkflowHistory) -> tuple[object, ...]:
    return StreamReadState.results.pop(
        history.events[
            0
        ].workflow_execution_started_event_attributes.original_execution_run_id
    )


async def _replay_failure(
    history: WorkflowHistory, workflow_class: type, **options: Any
) -> str:
    result = await Replayer(workflows=[workflow_class], **options).replay_workflow(
        history, raise_on_replay_failure=False
    )
    assert result.replay_failure is not None
    return str(result.replay_failure)


@_READ_CALLS
@pytest.mark.parametrize("codec", [False, True], ids=["no_codec", "codec"])
async def test_store_keeps_every_body_out_of_history_and_replays(
    client: Client, call: str, codec: bool
):
    data_converter = DataConverter(payload_codec=XorCodec()) if codec else None
    store = RequestsStore(MemoryStreamStore())
    history_client = _client(client, store, _HISTORY, data_converter)
    store_client = _client(client, store, _STORE, data_converter)
    stream_id = await _stream_of(store_client, *_CALL_TOKENS)
    args = CallArgs(stream_id, call=call)
    history_run, _ = await _run(history_client, QuietCallWorkflow, args)
    by_history = _run_result(history_run)
    history, count = await _run(store_client, QuietCallWorkflow, args)
    first_run = _run_result(history)
    # A first run gives Workflow code the same result under both settings.
    assert first_run == by_history
    kept = [0, 2, 4, 5] if call == "filter" else [0, 1, 2, 3, 4, 5]
    assert first_run[0] == tuple(_CALL_TOKENS[index].text for index in kept)
    assert count == len(kept)
    # A codec hides the bodies under HISTORY too, and without one they show there.
    assert _no_body_in(history_run) == codec
    assert _no_body_in(history)
    marker = await _plain_marker(history, data_converter)
    assert _replays_from_store(marker)
    assert _markers(history) == ["core_local_activity"]

    replayer_options: dict[str, Any] = {}
    if data_converter is not None:
        replayer_options["data_converter"] = data_converter
    store.reads.clear()
    await Replayer(
        workflows=[QuietCallWorkflow],
        stream_config=_config(store, _HISTORY),
        **replayer_options,
    ).replay_workflow(history)
    assert _run_result(history) == first_run
    if call == "filter":
        # One read of one Record for each kept Record, after the offset before it. The
        # two dropped Records are never read.
        assert [request.max_records for request in store.reads] == [1] * len(kept)
        offsets = [record.offset for record in await _store_records(store, stream_id)]
        assert [request.from_offset for request in store.reads] == [
            offsets[index - 1] if index else b"" for index in kept
        ]
    else:
        assert len(store.reads) == 1


@pytest.mark.parametrize("call", ["take", "filter"])
async def test_measure_store_marker(client: Client, call: str):
    sizes: dict[StreamReplaySource, int] = {}
    for replay_from in (_HISTORY, _STORE):
        store_client = _client(client, MemoryStreamStore(), replay_from)
        if call == "take":
            stream_id = await _stream_of(store_client, *_tokens(40), closed=True)
            history, count = await _run(
                store_client, QuietCallWorkflow, CallArgs(stream_id, until_kind=None)
            )
            assert count == 40
        else:
            tokens = [
                Token("hit" if index % 8 == 0 else "text", f"{_BODY_MARK}-{index}")
                for index in range(40)
            ]
            stream_id = await _stream_of(store_client, *tokens, closed=True)
            history, kept = await _run(store_client, KeepHitsWorkflow, stream_id)
            assert kept == 5
        StreamReadState.results.clear()
        payload = await _plain_marker(history)
        assert _replays_from_store(payload) == (replay_from is _STORE)
        assert _no_body_in(history) == (replay_from is _STORE)
        sizes[replay_from] = payload.ByteSize()
    label = (
        "take of 40 small Records" if call == "take" else "filter that keeps 5 of 40"
    )
    print(
        f"\nMarker of a {label}: HISTORY {sizes[_HISTORY]} bytes, "
        f"STORE {sizes[_STORE]} bytes"
    )
    assert sizes[_STORE] < 600
    assert sizes[_STORE] < sizes[_HISTORY]


async def _store_history(
    client: Client, call: str
) -> tuple[DictStreamStore, str, WorkflowHistory]:
    store = DictStreamStore()
    store_client = _client(client, store, _STORE)
    stream_id = await _stream_of(store_client, *_CALL_TOKENS)
    history, _ = await _run(store_client, CallWorkflow, CallArgs(stream_id, call=call))
    StreamReadState.results.clear()
    return store, stream_id, history


def _describe(call: str, stream_id: str) -> str:
    where = f"the Stream '{stream_id}' on the Store 'dict'"
    if call == "take":
        return f"{where}, 6 Records after the beginning up to 0000000000000006"
    return (
        f"{where}, 4 Records at the offsets 0000000000000001, 0000000000000003, "
        "0000000000000005, 0000000000000006"
    )


@_READ_CALLS
async def test_replay_without_the_records_fails_the_task(client: Client, call: str):
    store, stream_id, history = await _store_history(client, call)
    prefix = f"The replay of a {call} can't read {_describe(call, stream_id)} from the Store: "

    no_config = await _replay_failure(history, CallWorkflow)
    assert 'r#type: "StreamReplayError"' in no_config
    assert prefix + "the Worker has no Stream config" in no_config

    del store.streams[stream_id]
    deleted = await _replay_failure(
        history, CallWorkflow, stream_config=_config(store, _STORE)
    )
    assert 'r#type: "StreamReplayError"' in deleted
    assert prefix + f"StreamError: No Stream '{stream_id}'" in deleted


@_READ_CALLS
async def test_changed_body_fails_the_replay_through_the_digest(
    client: Client, call: str
):
    store, stream_id, history = await _store_history(client, call)
    record = store.streams[stream_id].records[2]
    record.body.data = record.body.data.replace(b"t2", b"t9")
    failure = await _replay_failure(
        history, CallWorkflow, stream_config=_config(store, _HISTORY)
    )
    assert 'r#type: "StreamReplayError"' in failure
    assert (
        f"The replay of a {call} can't read {_describe(call, stream_id)} from the "
        "Store: the Records differ from the digest of the first run"
    ) in failure


@workflow.defn
class TwoCallsWorkflow:
    def __init__(self) -> None:
        self.go = False

    @workflow.signal
    def proceed(self) -> None:
        self.go = True

    @workflow.run
    async def run(self, args: CallArgs) -> list[CallResult]:
        first = await _call(args)
        await workflow.wait_condition(lambda: self.go)
        second = await _call(dataclasses.replace(args, from_offset=first.last_offset))
        StreamReadState.results[workflow.info().run_id] = (
            tuple(first.texts),
            tuple(second.texts),
        )
        return [first, second]


@_READ_CALLS
@pytest.mark.parametrize(
    "order",
    [(_HISTORY, _STORE), (_STORE, _HISTORY)],
    ids=["history_first", "store_first"],
)
async def test_change_of_the_setting_keeps_a_running_workflow(
    client: Client, call: str, order: tuple[StreamReplaySource, StreamReplaySource]
):
    store = MemoryStreamStore()
    first_client = _client(client, store, order[0])
    second_client = _client(client, store, order[1])
    stream_id = await _stream_of(first_client, *_CALL_TOKENS, _done("again"))
    task_queue = f"tq-{uuid.uuid4()}"
    async with new_worker(first_client, TwoCallsWorkflow, task_queue=task_queue):
        handle = await first_client.start_workflow(
            TwoCallsWorkflow.run,
            CallArgs(stream_id, call=call),
            id=f"workflow-{uuid.uuid4()}",
            task_queue=task_queue,
        )

        async def first_call_recorded() -> None:
            assert _markers(await handle.fetch_history()) == ["core_local_activity"]

        await assert_eventually(first_call_recorded)
    # A new Worker with the other setting replays the first call and runs the second.
    async with new_worker(second_client, TwoCallsWorkflow, task_queue=task_queue):
        await handle.signal(TwoCallsWorkflow.proceed)
        first, second = await handle.result()
    kept = [0, 2, 4, 5] if call == "filter" else [0, 1, 2, 3, 4, 5]
    assert first.texts == [_CALL_TOKENS[index].text for index in kept]
    assert second.texts == [_done("again").text]
    history = await handle.fetch_history()
    markers = [await _plain_marker(history, index=index) for index in (0, 1)]
    assert [_replays_from_store(marker) for marker in markers] == [
        replay_from is _STORE for replay_from in order
    ]
    run_result = StreamReadState.results.pop(handle.result_run_id or "")
    for replay_from in (_HISTORY, _STORE):
        await Replayer(
            workflows=[TwoCallsWorkflow], stream_config=_config(store, replay_from)
        ).replay_workflow(history)
        assert StreamReadState.results.pop(handle.result_run_id or "") == run_result


async def test_reduce_is_the_same_under_both_settings(client: Client):
    store = MemoryStreamStore()
    stream_id = await _stream_of(_client(client, store, _HISTORY), *_CALL_TOKENS)
    markers: list[Payload] = []
    results: list[CallResult] = []
    for replay_from in (_HISTORY, _STORE):
        history, result = await _run(
            _client(client, store, replay_from),
            CallWorkflow,
            CallArgs(stream_id, call="reduce"),
        )
        StreamReadState.results.clear()
        markers.append(await _plain_marker(history))
        results.append(result)
        # The value of a fold is in History, so a replay needs no Store.
        await Replayer(workflows=[CallWorkflow]).replay_workflow(history)
        StreamReadState.results.clear()
    assert results[0] == results[1]
    assert results[0].texts == _texts(_CALL_TOKENS)
    assert markers[0] == markers[1]
    assert not _replays_from_store(markers[1])


async def test_take_over_one_mib_with_store(client: Client):
    store = MemoryStreamStore()
    store_client = _client(client, store, _STORE)
    tokens = [Token("text", f"{index}-" + "x" * 20_000) for index in range(60)]
    stream_id = await _stream_of(store_client, *tokens, closed=True)
    history, result = await _run(
        store_client, CallWorkflow, CallArgs(stream_id, until_kind=None)
    )
    # No default cap and no limit of 1 MiB, so one take gives every Record.
    assert (result.count, result.stop, result.done) == (60, "Done", True)
    assert result.texts == _texts(tokens)
    first_run = _run_result(history)
    marker = await _plain_marker(history)
    assert _replays_from_store(marker)
    assert marker.ByteSize() < 1000
    header = Payloads.FromString(marker.data).payloads[0].data.decode()
    assert '"count":60' in header
    await Replayer(
        workflows=[CallWorkflow], stream_config=_config(store, _STORE)
    ).replay_workflow(history)
    assert _run_result(history) == first_run
    # A take with a cap of its own keeps it.
    history, capped = await _run(
        store_client,
        CallWorkflow,
        CallArgs(stream_id, until_kind=None, max_bytes=100_000),
    )
    StreamReadState.results.clear()
    assert (capped.count, capped.stop) == (4, "MaxBytes")
    # Under HISTORY the default cap of 256 KiB holds.
    _, by_history = await _run(
        _client(client, store, _HISTORY),
        CallWorkflow,
        CallArgs(stream_id, until_kind=None),
    )
    StreamReadState.results.clear()
    assert by_history.stop == "MaxBytes"
    assert by_history.count < 60
