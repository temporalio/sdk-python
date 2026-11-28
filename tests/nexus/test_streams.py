"""Tests of the client path of Streams, with no server."""

import asyncio
import concurrent.futures
import copy
import dataclasses
import pickle
import pkgutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest
from google.protobuf.message import Message
from google.protobuf.timestamp_pb2 import Timestamp

import temporalio.api.streamservice.v1 as proto
import temporalio.client
import temporalio.converter
import temporalio.nexus.system
import temporalio.workflow
from temporalio.activity import WorkflowTaskAttempt
from temporalio.api.enums.v1 import StreamErrorReason, StreamIdConflictPolicy
from temporalio.api.failure.v1 import Failure
from temporalio.api.stream.v1 import (
    AppendRecord,
    ClientProducer,
    Producer,
    StreamErrorDetails,
)
from temporalio.client import (
    Client,
    NexusOperationHandle,
    StreamConfig,
    StreamSelectContext,
)
from temporalio.contrib.streams import as_producer, current_producer
from temporalio.converter import StorageDriverActivityInfo
from temporalio.exceptions import ApplicationError, StreamError
from temporalio.nexus.system import _support
from temporalio.nexus.system.stream_service import (
    ReadRecordsRequest,
    ReadRecordsResponse,
    StreamRecord,
    StreamRef,
)
from temporalio.nexus.system.stream_service import (
    StreamIdConflictPolicy as ConflictPolicy,
)
from temporalio.service import ServiceClient
from temporalio.testing import ActivityEnvironment
from temporalio.testing._stream_store import _MemoryStreams
from temporalio.worker._workflow_streams import _stream_error_from_failure
from tests.helpers.streams import (
    FaultStore,
    Hold,
    answers,
    core_failure,
    failure_note,
    loses_answer,
    run_with_virtual_clock,
)

_UNAVAILABLE = StreamErrorReason.STREAM_ERROR_REASON_STORE_UNAVAILABLE
_INVALID_ARGUMENT = StreamErrorReason.STREAM_ERROR_REASON_INVALID_ARGUMENT


class _Interceptor(temporalio.client.Interceptor):
    """Gives a client the outbound interceptor that ``outbound`` makes."""

    def __init__(
        self,
        outbound: Callable[
            [temporalio.client.OutboundInterceptor],
            temporalio.client.OutboundInterceptor,
        ],
    ) -> None:
        super().__init__()
        self._outbound = outbound

    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        return self._outbound(next)


class _RecordingOutbound(temporalio.client.OutboundInterceptor):
    """Logs the Store name of the ref of each Stream request before and after its
    call.
    """

    def __init__(
        self,
        next: temporalio.client.OutboundInterceptor,
        stores: list[tuple[str | None, str | None]],
    ) -> None:
        super().__init__(next)
        self._stores = stores

    async def start_nexus_operation(
        self, input: temporalio.client.StartNexusOperationInput
    ) -> NexusOperationHandle[Any]:
        ref = getattr(input.arg, "stream_ref", None) or input.arg.ref
        before = ref.store
        try:
            return await self.next.start_nexus_operation(input)
        finally:
            self._stores.append((before, ref.store))


def _producer_proto(producer: Any) -> Producer:
    converter = temporalio.converter.DataConverter.default
    message = _support.model_to_proto(
        producer, converter.payload_converter, converter.failure_converter
    )
    assert isinstance(message, Producer)
    return message


@pytest.mark.parametrize("operation", ["create", "append", "read", "describe"])
@pytest.mark.parametrize(
    "routing", ["only", "selector", "explicit", "unknown", "unregistered"]
)
@pytest.mark.parametrize("in_activity", [False, True])
async def test_a_client_call_reaches_the_selected_store(
    operation: str, routing: str, in_activity: bool
) -> None:
    service_client = Mock(spec=ServiceClient)
    a, b = FaultStore("a"), FaultStore("b")
    stores = [a] if routing == "only" else [a, b]
    selected = FaultStore("other") if routing == "unregistered" else b
    selects: list[StreamSelectContext] = []

    def selector(context: StreamSelectContext) -> FaultStore:
        selects.append(context)
        return selected

    ref_store = {"explicit": "a", "unknown": "unknown"}.get(routing)
    intercepted: list[tuple[str | None, str | None]] = []
    client = Client(
        service_client,
        interceptors=[_Interceptor(lambda next: _RecordingOutbound(next, intercepted))],
        stream_config=StreamConfig(
            stores, store_selector=None if routing == "only" else selector
        ),
    )
    activity = ActivityEnvironment()
    activity.info = dataclasses.replace(
        activity.info, activity_id="activity", activity_run_id="run"
    )
    producer = (
        _producer_proto(activity.run(current_producer))
        if in_activity
        else Producer(
            client=ClientProducer(namespace="default", id=client._stream_producer_id)
        )
    )
    for store in stores:
        await store.memory.create_stream(
            proto.CreateStreamRequest(stream_id="s"), request_id="setup"
        )
        store.faults["read_records"].extend([answers(proto.ReadRecordsResponse())] * 2)
    stream = client.get_stream(StreamRef[int](id="s", store=ref_store))
    payload = temporalio.converter.PayloadConverter.default.to_payload
    expected: Message
    if operation == "create":
        expected = proto.CreateStreamRequest(
            stream_id="s",
            id_conflict_policy=StreamIdConflictPolicy.STREAM_ID_CONFLICT_POLICY_USE_EXISTING,
        )
    elif operation == "append":
        expected = proto.AppendRecordsRequest(
            stream_id="s",
            producer=producer,
            records=[AppendRecord(body=payload(i)) for i in (3, 5)],
        )
    elif operation == "read":
        expected = proto.ReadRecordsRequest(
            stream_id="s", max_records=2, max_bytes=4096
        )
        expected.wait.FromSeconds(5)
    else:
        expected = proto.DescribeStreamRequest(stream_id="s")

    async def call() -> Any:
        if operation == "create":
            return await client.create_stream(
                id="s",
                store=ref_store,
                body_type=int,
                id_conflict_policy=ConflictPolicy.StreamIdConflictPolicyUseExisting,
            )
        if operation == "append":
            return await stream.append(3, 5)
        if operation == "read":
            return await stream.read_page(
                max_records=2, max_bytes=4096, wait=timedelta(seconds=5)
            )
        return await stream.describe()

    if routing in ("unknown", "unregistered"):
        with pytest.raises(ValueError):
            await (activity.run(call) if in_activity else call())
        assert not a.calls and not b.calls and not service_client.mock_calls
        return

    for _ in range(2):
        result = await (activity.run(call) if in_activity else call())
        if operation == "create":
            assert result.created is False
            assert result.ref == StreamRef(
                id="s", store="b" if routing == "selector" else "a"
            )
    chosen, other = (b, a) if routing == "selector" else (a, b)
    assert chosen.requests == [expected, expected]
    assert not other.calls and not service_client.mock_calls
    ids = [request_id for _, request_id in chosen.calls]
    if operation in ("create", "append"):
        assert all(ids) and ids[0] != ids[1]
    else:
        assert ids == [None, None]
    assert intercepted == [(ref_store, ref_store)] * 2
    target = (
        StorageDriverActivityInfo(
            namespace="default", id="activity", run_id="run", type="unknown"
        )
        if in_activity
        else None
    )
    assert selects == (
        [StreamSelectContext(id="s", target=target, namespace="default")] * 2
        if routing == "selector"
        else []
    )


async def test_a_client_without_stream_config_does_not_call_the_server() -> None:
    service_client = Mock(spec=ServiceClient)
    with pytest.raises(ValueError):
        await Client(service_client).create_stream(id="s")
    assert not service_client.mock_calls


async def test_a_created_stream_keeps_its_selected_store() -> None:
    a, b = FaultStore("a"), FaultStore("b")
    selected = a
    client = Client(
        Mock(spec=ServiceClient),
        stream_config=StreamConfig([a, b], store_selector=lambda _: selected),
    )
    stream = await client.create_stream(id="s", body_type=int)
    selected = b
    await stream.append(1)
    await stream.describe()
    assert stream.ref.store == "a"
    assert [type(request) for request in a.requests] == [
        proto.CreateStreamRequest,
        proto.AppendRecordsRequest,
        proto.DescribeStreamRequest,
    ]
    assert not b.calls


async def test_a_client_retry_recovers_a_lost_store_answer() -> None:
    class Retry(temporalio.client.OutboundInterceptor):
        async def start_nexus_operation(
            self, input: temporalio.client.StartNexusOperationInput
        ) -> NexusOperationHandle[Any]:
            try:
                return await self.next.start_nexus_operation(dataclasses.replace(input))
            except StreamError as err:
                if err.reason != _UNAVAILABLE:
                    raise
            return await self.next.start_nexus_operation(dataclasses.replace(input))

    store = FaultStore()
    store.faults["create_stream"].append(loses_answer())
    store.faults["append_records"].append(loses_answer())
    client = Client(
        Mock(spec=ServiceClient),
        stream_config=StreamConfig([store]),
        interceptors=[_Interceptor(Retry)],
    )
    stream = await client.create_stream(id="s", body_type=int)
    assert stream.created is True
    await stream.append(1)
    await stream.append(2)
    create, create_retry, append, append_retry, second = (key for _, key in store.calls)
    assert create == create_retry
    assert append == append_retry
    assert len({create, append, second}) == 3
    assert [record.body for record in (await stream.read_page()).records] == [1, 2]


async def test_an_empty_client_append_does_not_reach_the_store() -> None:
    store = FaultStore()
    client = Client(Mock(spec=ServiceClient), stream_config=StreamConfig([store]))
    with pytest.raises(StreamError) as raised:
        await client.get_stream(StreamRef[int](id="s")).append()
    assert raised.value.reason == _INVALID_ARGUMENT
    assert not store.calls


async def test_a_stream_error_keeps_its_reason_on_every_path() -> None:
    converter = temporalio.converter.DataConverter.default
    for number in [*StreamErrorReason.values(), 99]:
        known = number in StreamErrorReason.values()
        reason = StreamErrorReason.ValueType(number if known else 0)
        token = (
            "stream_"
            + StreamErrorReason.Name(reason)
            .removeprefix("STREAM_ERROR_REASON_")
            .lower()
        )
        message = f"The call failed with {number}"
        expected = (reason, token, message, reason != _UNAVAILABLE)
        details = StreamErrorDetails(reason=number)  # type: ignore[arg-type]

        # Through an Activity, which gives its error to the default failure converter.
        raised: ApplicationError = (
            StreamError(message, reason=reason)
            if known
            else ApplicationError(message, details, type=token, non_retryable=True)
        )
        failure = Failure()
        converter.failure_converter.to_failure(
            raised, converter.payload_converter, failure
        )
        # From Core, which builds the failure of a Stream job itself.
        from_core = core_failure(number, message)
        for form in (failure, from_core):
            error = _stream_error_from_failure(form)
            assert error is not None, number
            # A copy of the error, also one from another process, is the same error.
            for same in (error, copy.copy(error), pickle.loads(pickle.dumps(error))):
                assert (
                    same.reason,
                    same.type,
                    same.message,
                    same.non_retryable,
                ) == expected, number
        if not known:
            # A newer Worker can record a reason by a name that this SDK doesn't have.
            named = failure.application_failure_info.details.payloads[0]
            named.data = b'{"reason": "STREAM_ERROR_REASON_ADDED_LATER"}'
            later = _stream_error_from_failure(failure)
            assert later and (later.reason, later.message) == (reason, message)
    assert _stream_error_from_failure(Failure(message="other")) is None


class _FakeReads(temporalio.client.OutboundInterceptor):
    def __init__(self, next: temporalio.client.OutboundInterceptor) -> None:
        super().__init__(next)
        self.pages = iter([(1, 2), (), (3, 4), (), (4, 5)])
        self.starts: list[int] = []
        self.waits: list[timedelta | None] = []

    async def start_nexus_operation(
        self, input: temporalio.client.StartNexusOperationInput
    ) -> NexusOperationHandle[Any]:
        request: ReadRecordsRequest[int] = input.arg
        self.starts.append(int.from_bytes(request.from_offset or b"", "big"))
        self.waits.append(request.wait)
        records = [
            StreamRecord(body=at, offset=at.to_bytes(8, "big"))
            for at in next(self.pages)
        ]
        page = ReadRecordsResponse(
            records=records,
            last_offset=records[-1].offset if records else request.from_offset,
        )
        handle: NexusOperationHandle[Any] = NexusOperationHandle(Mock(), input.id)
        handle._known_outcome = page
        return handle


@pytest.mark.parametrize("wait", [None, timedelta(0), timedelta(seconds=5)])
async def test_a_read_resumes_after_a_partial_page_and_skips_empty_pages(
    wait: timedelta | None,
) -> None:
    fake: list[_FakeReads] = []

    def outbound(next: temporalio.client.OutboundInterceptor) -> _FakeReads:
        fake.append(_FakeReads(next))
        return fake[0]

    client = Client(Mock(spec=ServiceClient), interceptors=[_Interceptor(outbound)])
    stream = client.get_stream(StreamRef[int](id="s"))
    [reads] = fake
    got: list[int] = []
    later = _support.DEFAULT_READ_WAIT if wait is None else wait
    for stop_at in (3, 5):
        offset = got[-1].to_bytes(8, "big") if got else None
        async for record in stream.read(from_offset=offset, wait=wait):
            got.append(record.body)
            if record.body == stop_at:
                break
    assert got == [1, 2, 3, 4, 5]
    assert reads.starts == [0, 2, 2, 3, 3]
    assert reads.waits == [wait, later, later, wait, later]


@dataclasses.dataclass(frozen=True)
class _WriterInfo:
    kind: str
    namespace: str
    workflow_id: str
    run_id: str
    activity_id: str
    attempt: int
    time: datetime
    event_id: int


def _default_producer(writer: _WriterInfo) -> Any:
    """Return the default Producer of a write of ``writer``."""
    if writer.kind == "workflow":
        info = SimpleNamespace(
            namespace=writer.namespace,
            workflow_id=writer.workflow_id,
            run_id=writer.run_id,
        )
        with patch("temporalio.workflow.info", lambda: info):
            return _support.workflow_producer()
    in_workflow = writer.kind != "standalone_activity"
    activity = ActivityEnvironment()
    activity.info = dataclasses.replace(
        activity.info,
        namespace=writer.namespace,
        activity_id=writer.activity_id,
        attempt=writer.attempt,
        current_attempt_scheduled_time=writer.time,
        is_local=writer.kind == "local_activity",
        workflow_id=writer.workflow_id if in_workflow else None,
        workflow_run_id=writer.run_id if in_workflow else None,
        activity_run_id=None if in_workflow else writer.run_id,
        workflow_task=WorkflowTaskAttempt(
            writer.event_id, writer.attempt + 1, writer.time
        )
        if writer.kind == "local_activity"
        else None,
        _scheduled_event_id=writer.event_id
        if writer.kind == "workflow_activity"
        else 0,
    )
    producer = activity.run(current_producer)
    # The default of a write in an Activity is that Producer, and it reads no client.
    assert activity.run(_support.default_producer, cast(Any, None)) == producer
    return producer


def _expected_producer(writer: _WriterInfo) -> Producer:
    """Return the whole Producer of ``writer``, built field by field."""
    time = Timestamp()
    time.FromDatetime(writer.time)
    producer = Producer()
    kind = getattr(producer, writer.kind)
    kind.namespace, kind.run_id = writer.namespace, writer.run_id
    if writer.kind != "standalone_activity":
        kind.workflow_id = writer.workflow_id
    if writer.kind == "workflow":
        return producer
    kind.activity_id = writer.activity_id
    kind.attempt.attempt = writer.attempt
    if writer.kind == "local_activity":
        task = kind.attempt.task
        task.started_event_id, task.attempt = writer.event_id, writer.attempt + 1
        task.started_time.CopyFrom(time)
    else:
        kind.attempt.attempt_scheduled_time.CopyFrom(time)
    if writer.kind == "workflow_activity":
        kind.scheduled_event_id = writer.event_id
    return producer


@pytest.mark.parametrize(
    ("kind", "event_id"),
    [
        ("workflow", 7),
        ("local_activity", 9),
        ("workflow_activity", 0),
        ("workflow_activity", 17),
        ("standalone_activity", 0),
    ],
)
def test_a_writer_keeps_its_identity_and_attempt(kind: str, event_id: int) -> None:
    writer = _WriterInfo(
        kind=kind,
        namespace="ns:/é",
        workflow_id="workflow",
        run_id="run",
        activity_id="activity",
        attempt=3,
        time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        event_id=event_id,
    )
    producer = _default_producer(writer)
    assert _producer_proto(producer) == _expected_producer(writer)
    converter = temporalio.converter.PayloadConverter.default
    # Heartbeat details can be decoded without a type hint.
    [stored] = converter.from_payloads(converter.to_payloads([producer]))
    assert as_producer(stored) == producer
    assert as_producer(producer) is producer


def test_a_client_uses_one_producer_for_all_its_streams() -> None:
    service_client = Mock(spec=ServiceClient)
    config = StreamConfig(stores=[FaultStore()])
    clients = [Client(service_client, stream_config=config) for _ in range(2)]
    [first, second, third] = [
        _producer_proto(
            _support.default_producer(client.get_stream(StreamRef(id=id))._nexus_client)
        )
        for client, id in ((clients[0], "a"), (clients[0], "b"), (clients[1], "a"))
    ]
    assert first == second != third
    assert first.client.namespace == "default" and first.client.id


@dataclasses.dataclass
class _Turn:
    text: str


@pytest.mark.parametrize(
    ("hint", "ref", "body_type"),
    [
        (StreamRef[int], StreamRef[int](id="count", store="a"), int),
        (StreamRef[str], StreamRef[str](id="text"), str),
        (StreamRef[_Turn], StreamRef[_Turn](id="turn", store="b"), _Turn),
        (StreamRef, StreamRef(id="untyped"), object),
        (StreamRef[int] | None, None, None),
        (StreamRef[str] | None, StreamRef[str](id="optional"), str),
    ],
)
def test_a_stream_ref_keeps_its_body_type(
    hint: Any, ref: StreamRef[Any] | None, body_type: type | None
) -> None:
    converter = temporalio.converter.DataConverter.default.payload_converter
    client = Client(
        Mock(spec=ServiceClient), stream_config=StreamConfig([FaultStore()])
    )
    [decoded] = converter.from_payloads(converter.to_payloads([ref]), [hint])
    assert decoded == ref
    if decoded is not None:
        for get_stream in (client.get_stream, temporalio.workflow.get_stream):
            assert get_stream(decoded).body_type is body_type


@dataclasses.dataclass
class _RefFields:
    typed: StreamRef[_Turn]
    untyped: StreamRef
    nested: list[StreamRef[int] | None]


def test_stream_ref_fields_keep_their_body_types_inside_lists() -> None:
    value = [
        _RefFields(
            typed=StreamRef[_Turn](id="turn", store="a"),
            untyped=StreamRef(id="untyped"),
            nested=[StreamRef[int](id="count"), None],
        )
    ]
    converter = temporalio.converter.DataConverter.default.payload_converter
    client = Client(
        Mock(spec=ServiceClient), stream_config=StreamConfig([FaultStore()])
    )
    [decoded] = converter.from_payloads(
        converter.to_payloads([value]), [list[_RefFields]]
    )
    assert decoded == value
    [fields] = decoded
    assert fields.nested[1] is None
    for get_stream in (client.get_stream, temporalio.workflow.get_stream):
        assert get_stream(fields.typed).body_type is _Turn
        assert get_stream(fields.untyped).body_type is object
        assert get_stream(fields.nested[0]).body_type is int


def test_each_stream_module_can_be_the_first_import() -> None:
    system = temporalio.nexus.system
    modules = [
        "temporalio.client._stream_store",
        "temporalio.testing._stream_store",
        "temporalio.contrib.streams",
        "temporalio.workflow",
        system.__name__,
        *(
            module.name
            for module in pkgutil.walk_packages(system.__path__, system.__name__ + ".")
        ),
    ]

    def first_import(module: str) -> str:
        imported = subprocess.run(
            [sys.executable, "-c", f"import {module}"], capture_output=True, text=True
        )
        return imported.stderr if imported.returncode else ""

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        for module, error in zip(modules, pool.map(first_import, modules)):
            assert not error, f"{module}: {error}"


def test_a_client_call_past_its_rpc_timeout_cancels_the_store_call() -> None:
    async def script() -> None:
        loop = asyncio.get_running_loop()
        store, hold = FaultStore(), Hold()
        store.faults["describe_stream"].append(hold)
        client = Client(Mock(spec=ServiceClient), stream_config=StreamConfig([store]))
        stream = client.get_stream(StreamRef[str](id="s"))
        with pytest.raises(StreamError) as timed_out:
            await stream.describe(rpc_timeout=timedelta(seconds=5))
        assert timed_out.value.reason == _UNAVAILABLE
        assert loop.time() == 5
        assert hold.cancelled.is_set()
        assert asyncio.all_tasks() == {asyncio.current_task()}

    run_with_virtual_clock(script())


def test_a_client_iterator_on_an_idle_stream_waits_between_reads() -> None:
    # The Streams of a MemoryStreamStore with no call through the threads of Core, so
    # the virtual clock doesn't jump past a wait while an append is on its way.
    class ReadLog(_MemoryStreams):
        def __init__(self) -> None:
            super().__init__(
                "memory", retention=timedelta(days=1), resend_window=16, fence=False
            )
            self.reads: list[proto.ReadRecordsRequest] = []

        async def read_records(
            self, request: proto.ReadRecordsRequest
        ) -> proto.ReadRecordsResponse:
            self.reads.append(request)
            return await super().read_records(request)

    def waits(store: ReadLog) -> list[timedelta | None]:
        return [
            request.wait.ToTimedelta() if request.HasField("wait") else None
            for request in store.reads
        ]

    async def script() -> None:
        loop = asyncio.get_running_loop()
        store = ReadLog()
        client = Client(Mock(spec=ServiceClient), stream_config=StreamConfig([store]))
        stream = await client.create_stream(id="s", body_type=int)
        minute = _support.DEFAULT_READ_WAIT
        ten, nine = timedelta(seconds=10), timedelta(seconds=9)
        # With no wait, the first read answers at once and each later one waits the
        # default wait. A wait of zero is the caller's choice to read in a loop, and
        # the Store gets no wait for it. A wait ends before the rpc_timeout, so the
        # Store answers in time.
        cases: list[tuple[timedelta | None, timedelta | None, list[Any]]] = [
            (None, None, [None, minute, minute, minute, minute]),
            (ten, None, [ten] * 5),
            (timedelta(0), None, [None] * 5),
            (None, ten, [None, nine, nine, nine, nine]),
            (minute, ten, [nine] * 5),
            (timedelta(seconds=5), ten, [timedelta(seconds=5)] * 5),
            # A Store has a tenth of a short timeout to answer, and a timeout of
            # zero is no timeout.
            (ten, timedelta(seconds=2), [timedelta(seconds=1.8)] * 5),
            (ten, timedelta(0), [ten] * 5),
        ]
        for wait, rpc_timeout, expected in cases:
            with failure_note(f"wait {wait}, rpc_timeout {rpc_timeout}"):
                store.reads.clear()
                start = loop.time()
                pages = stream.read_pages(wait=wait, rpc_timeout=rpc_timeout)
                for _ in expected:
                    assert not (await anext(pages)).records
                await pages.aclose()
                assert waits(store) == expected
                assert loop.time() - start == pytest.approx(
                    sum(w.total_seconds() for w in expected if w)
                )

        # A Record that comes during a wait ends it at once, and the caller stops.
        async def append_later() -> None:
            await asyncio.sleep(90)
            await stream.append(1)

        store.reads.clear()
        start = loop.time()
        appending = asyncio.create_task(append_later())
        async with asyncio.timeout(600):
            async for record in stream.read():
                assert record.body == 1
                break
        await appending
        assert loop.time() - start == pytest.approx(90)
        assert waits(store) == [None, minute, minute]

    run_with_virtual_clock(script())


@pytest.mark.parametrize(
    "offsets, last_offset, expected",
    [
        ([b"b", b"c"], b"d", b"d"),
        ([b"b", b"c"], None, b"c"),
        ([], None, b"a"),
    ],
)
def test_the_next_read_uses_the_position_covered_by_the_page(
    offsets: list[bytes], last_offset: bytes | None, expected: bytes
) -> None:
    request = ReadRecordsRequest(ref=StreamRef[int](id="s"), from_offset=b"a")
    page = ReadRecordsResponse(
        records=[
            StreamRecord(body=i, offset=offset) for i, offset in enumerate(offsets)
        ],
        last_offset=last_offset,
    )
    next_request = _support.next_read_request(request, page)
    assert next_request is not None and next_request.from_offset == expected


def test_a_page_with_no_last_offset_ends_at_its_last_record() -> None:
    async def unset(call: Callable[[], Awaitable[Any]]) -> Any:
        page = await call()
        page.ClearField("last_offset")
        return page

    async def script() -> None:
        store = FaultStore()
        client = Client(Mock(spec=ServiceClient), stream_config=StreamConfig([store]))
        stream = await client.create_stream(id="s", body_type=int)
        await stream.append(1, 2)
        store.faults["read_records"].extend([unset, unset])
        page = await stream.read_page()
        assert page.last_offset == page.records[-1].offset
        # A page with no Record ends where its read started.
        empty = await stream.read_page(from_offset=page.last_offset)
        assert (empty.records, empty.last_offset) == ([], page.last_offset)

    run_with_virtual_clock(script())


@pytest.mark.parametrize("by_page", [False, True])
async def test_zero_wait_poll_can_be_cancelled_after_an_empty_page(
    by_page: bool,
) -> None:
    class IdleStore(_MemoryStreams):
        reads: int = 0

        async def read_records(
            self, request: proto.ReadRecordsRequest
        ) -> proto.ReadRecordsResponse:
            assert not request.HasField("wait")
            self.reads += 1
            assert self.reads == 1, "another poll ran before the pending cancellation"
            return proto.ReadRecordsResponse()

    store = IdleStore(
        "memory", retention=timedelta(days=1), resend_window=16, fence=False
    )
    client = Client(Mock(spec=ServiceClient), stream_config=StreamConfig([store]))
    stream = await client.create_stream(id="s", body_type=int)

    async def consume() -> None:
        if by_page:
            async for page in stream.read_pages(wait=timedelta(0)):
                assert not page.records
        else:
            _ = await anext(stream.read(wait=timedelta(0)))

    task = asyncio.create_task(consume())
    _ = asyncio.get_running_loop().call_soon(task.cancel)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.reads == 1
