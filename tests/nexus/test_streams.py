"""Tests of the client path of Streams, with no server."""

import asyncio
import concurrent.futures
import copy
import dataclasses
import pickle
import pkgutil
import random
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
    core_failure,
    failure_note,
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


async def test_a_client_call_reaches_the_right_store_as_written() -> None:
    request_ids: set[str] = set()
    for seed in range(300):
        with failure_note(f"seed {seed}"):
            await _routing_script(seed, random.Random(seed), request_ids)


async def _routing_script(seed: int, rng: random.Random, request_ids: set[str]) -> None:
    to_payload = temporalio.converter.PayloadConverter.default.to_payload
    service_client = Mock(spec=ServiceClient)
    names = rng.sample("abcd", rng.randrange(1, 5))
    has_selector = len(names) > 1 or rng.random() < 0.5
    stores = [FaultStore(name) for name in names]
    selects: list[StreamSelectContext] = []
    # The Store that the selector returns at its next call, which may be unregistered.
    selected = [FaultStore("other")]

    def selector(context: StreamSelectContext) -> FaultStore:
        selects.append(context)
        return selected[0]

    # A client with no Stream config refuses a call, and the server gets nothing.
    with pytest.raises(ValueError):
        await Client(service_client).create_stream(id="s")
    assert not service_client.mock_calls
    ref_stores: list[tuple[str | None, str | None]] = []
    client = Client(
        service_client,
        interceptors=[_Interceptor(lambda next: _RecordingOutbound(next, ref_stores))],
        stream_config=StreamConfig(
            stores=stores, store_selector=selector if has_selector else None
        ),
    )
    activity = ActivityEnvironment()
    activity.info = dataclasses.replace(
        activity.info, activity_id=f"activity-{seed}", activity_run_id="run"
    )
    # A write has the Producer of its caller, which is the client or the Activity.
    producers = [
        Producer(
            client=ClientProducer(namespace="default", id=client._stream_producer_id)
        ),
        _producer_proto(activity.run(current_producer)),
    ]
    created: list[tuple[Any, FaultStore]] = []
    for step in range(12):
        with failure_note(f"step {step}"):
            selected[0] = rng.choice([*stores, *stores, FaultStore("other")])
            stream_id = f"stream-{seed}-{step}"
            ref_store = rng.choice([None, None, "unknown", *names])
            store: FaultStore | None
            in_activity = rng.random() < 0.3
            expected: Message
            operation = rng.choice(["create", "read", "describe", "created"])
            if operation == "created" and not created:
                operation = "create"
            if operation == "created":
                # A created Stream stays on its Store, whatever the selector picks at this call.
                stream, store = rng.choice(created)
                ref_store = stream.ref.store
                if rng.random() < 0.5:
                    bodies = [rng.randrange(100) for _ in range(rng.randrange(3))]
                    call = stream.append(*bodies)
                    expected = proto.AppendRecordsRequest(
                        stream_id=stream.ref.id,
                        producer=producers[in_activity],
                        records=[
                            AppendRecord(body=to_payload(body)) for body in bodies
                        ],
                    )
                else:
                    call = stream.describe()
                    expected = proto.DescribeStreamRequest(stream_id=stream.ref.id)
            else:
                store = next((s for s in stores if s.name() == ref_store), None)
                if ref_store is None and has_selector:
                    store = selected[0] if selected[0] in stores else None
                elif ref_store is None:
                    store = stores[0]
                ref = StreamRef[int](id=stream_id, store=ref_store)
                match operation:
                    case "create":
                        policy = rng.choice(list(StreamIdConflictPolicy.values()))
                        call = client.create_stream(
                            id=stream_id,
                            store=ref_store,
                            id_conflict_policy=ConflictPolicy(policy)
                            if policy
                            else None,
                            body_type=int,
                        )
                        expected = proto.CreateStreamRequest(
                            stream_id=stream_id, id_conflict_policy=policy
                        )
                    case "read":
                        expected = proto.ReadRecordsRequest(
                            stream_id=stream_id,
                            from_offset=rng.randbytes(rng.randrange(3)),
                            max_records=rng.choice([0, 1, 2**31 - 1]),
                            max_bytes=rng.choice([0, 1, 2**62]),
                        )
                        wait = rng.choice([None, 0, 1, 30 * 24 * 3600])
                        # A Store gets no wait for a wait of zero.
                        if wait:
                            expected.wait.FromSeconds(wait)
                        call = client.get_stream(ref).read_page(
                            from_offset=expected.from_offset or None,
                            max_records=expected.max_records or None,
                            wait=None if wait is None else timedelta(seconds=wait),
                            max_bytes=expected.max_bytes or None,
                        )
                    case _:
                        call = client.get_stream(ref).describe()
                        expected = proto.DescribeStreamRequest(stream_id=stream_id)
            logs = [list(s.requests) for s in stores]
            del selects[:], ref_stores[:]

            async def run_call() -> Any:
                # A read or a describe of a Stream that nobody created fails in the Store.
                try:
                    return await call
                except StreamError as err:
                    return err

            if store is None:
                with pytest.raises(ValueError):
                    await (activity.run(run_call) if in_activity else run_call())
                assert [s.requests for s in stores] == logs
                continue
            result = await (activity.run(run_call) if in_activity else run_call())
            if (
                isinstance(expected, proto.AppendRecordsRequest)
                and not expected.records
            ):
                # The SDK refuses a batch with no Record before the Store gets it.
                assert isinstance(result, StreamError)
                assert result.reason == _INVALID_ARGUMENT
                assert [s.requests for s in stores] == logs
                continue
            for other, log in zip(stores, logs):
                if other is not store:
                    assert other.requests == log
            [(request, request_id)] = store.calls[len(logs[stores.index(store)]) :]
            if isinstance(
                request, (proto.CreateStreamRequest, proto.AppendRecordsRequest)
            ):
                assert request_id and request_id not in request_ids
                request_ids.add(request_id)
            else:
                assert request_id is None
            assert request == expected
            assert not service_client.mock_calls
            # An interceptor sees the ref as the caller wrote it, also after the call.
            assert ref_stores == [(ref_store, ref_store)]
            target = None
            if in_activity:
                target = StorageDriverActivityInfo(
                    namespace="default",
                    id=f"activity-{seed}",
                    run_id="run",
                    type="unknown",
                )
            used_selector = has_selector and ref_store is None
            assert selects == (
                [
                    StreamSelectContext(
                        id=stream_id, target=target, namespace=client.namespace
                    )
                ]
                if used_selector
                else []
            )
            if operation == "create":
                assert result.ref == StreamRef(id=stream_id, store=store.name())
                created.append((result, store))


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
    """Answers each read with the next page of a generated Stream."""

    def __init__(
        self, next: temporalio.client.OutboundInterceptor, rng: random.Random
    ) -> None:
        super().__init__(next)
        self._rng = rng
        self.records = 0
        """The number of Records of the Stream, which a test can raise."""
        self.starts: list[int] = []
        """The Record after which each read started, in the order of the reads."""
        self.waits: list[timedelta | None] = []
        """The wait of each read, in the order of the reads."""

    async def start_nexus_operation(
        self, input: temporalio.client.StartNexusOperationInput
    ) -> NexusOperationHandle[Any]:
        request: ReadRecordsRequest[int] = input.arg
        start = int.from_bytes(request.from_offset or b"", "big")
        self.starts.append(start)
        self.waits.append(request.wait)
        size = self._rng.randrange(1, 4)
        # A read with a wait can see no Record come within its wait, also when more
        # come later. With no wait, a Store answers with no Record only at the end.
        if request.wait and self._rng.random() < 0.2:
            size = 0
        end = min(self.records, start + size)
        page = ReadRecordsResponse(
            records=[
                StreamRecord(body=at, offset=at.to_bytes(8, "big"))
                for at in range(start + 1, end + 1)
            ],
            last_offset=request.from_offset or None,
        )
        if page.records:
            page.last_offset = page.records[-1].offset
        handle: NexusOperationHandle[Any] = NexusOperationHandle(Mock(), input.id)
        handle._known_outcome = page
        return handle


async def test_a_read_gives_each_record_once_past_pages_with_no_record() -> None:
    for seed in range(300):
        with failure_note(f"seed {seed}"):
            await _read_script(random.Random(seed))


async def _read_script(rng: random.Random) -> None:
    fake: list[_FakeReads] = []

    def outbound(next: temporalio.client.OutboundInterceptor) -> _FakeReads:
        fake.append(_FakeReads(next, rng))
        return fake[0]

    client = Client(Mock(spec=ServiceClient), interceptors=[_Interceptor(outbound)])
    stream = client.get_stream(StreamRef[int](id="s"))
    [reads] = fake
    wait = rng.choice([None, timedelta(0), timedelta(seconds=5)])
    got: list[int] = []
    later = _support.DEFAULT_READ_WAIT if wait is None else wait
    for _ in range(rng.randrange(1, 4)):
        # More Records come after each read, and a reader picks up where it stopped.
        reads.records += rng.randrange(1, 6)
        offset = got[-1].to_bytes(8, "big") if got else None
        # A Stream has no end, so the reader stops at a Record that it knows of.
        stop_at = rng.randrange(len(got) + 1, reads.records + 1)
        first_read = len(reads.starts)
        async for record in stream.read(from_offset=offset, wait=wait):
            got.append(record.body)
            if len(got) == stop_at:
                break
        # A page with no Record before the caller's last Record doesn't end the read.
        assert got == list(range(1, stop_at + 1))
        # The read stops at the caller's last Record, so no read starts after it.
        assert all(start < stop_at for start in reads.starts[first_read:])
        # The first read has the caller's wait. A later one has it too, or the
        # default wait when the caller gave none.
        assert reads.waits[first_read:] == [wait] + [later] * (
            len(reads.waits) - first_read - 1
        )


@dataclasses.dataclass(frozen=True)
class _WriterInfo:
    """A generated writer of a Stream, with the fields that its Producer takes."""

    kind: str
    """The field of the kind in the proto ``Producer``."""
    namespace: str
    workflow_id: str
    run_id: str
    activity_id: str
    attempt: int
    time: datetime
    """The scheduled time of an Activity attempt, or the started time of a task."""
    event_id: int
    """The started event id of the task of a Local Activity, or the scheduled event id
    of an Activity of a Workflow, which is zero when SDK Core couldn't read it."""


def _writer_info(rng: random.Random) -> _WriterInfo:
    def text() -> str:
        return "".join(rng.choice("ab:/ '\"é") for _ in range(rng.randrange(1, 5)))

    kinds = ["workflow", "local_activity", "workflow_activity", "standalone_activity"]
    kind = rng.choice(kinds)
    return _WriterInfo(
        kind=kind,
        namespace=text(),
        workflow_id=text(),
        run_id=text(),
        activity_id=text(),
        attempt=rng.randrange(1, 9),
        time=datetime.fromtimestamp(rng.randrange(10**9), timezone.utc),
        event_id=rng.choice([0, rng.randrange(3, 99)])
        if kind == "workflow_activity"
        else rng.randrange(3, 99),
    )


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


def test_a_writer_is_one_producer_on_each_attempt() -> None:
    for seed in range(300):
        with failure_note(f"seed {seed}"):
            rng = random.Random(seed)
            writer = _writer_info(rng)
            producer = _producer_proto(_default_producer(writer))
            assert producer == _expected_producer(writer)

    # Two Streams of one client have one Producer, and two clients have two.
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


def test_a_stored_producer_comes_back_as_a_producer() -> None:
    converter = temporalio.converter.PayloadConverter.default
    for seed in range(50):
        with failure_note(f"seed {seed}"):
            producer = _default_producer(_writer_info(random.Random(seed)))
            # A value such as a heartbeat detail comes back with no type hint.
            [stored] = converter.from_payloads(converter.to_payloads([producer]))
            assert as_producer(stored) == producer
            assert as_producer(producer) is producer


@dataclasses.dataclass
class _Turn:
    text: str


def _refs(value: Any) -> list[Any]:
    """Return each ``StreamRef`` of a value, in the order of its lists and fields."""
    if isinstance(value, StreamRef):
        return [value]
    if isinstance(value, list):
        return [ref for item in value for ref in _refs(item)]
    if dataclasses.is_dataclass(value):
        fields = dataclasses.fields(value)
        return _refs([getattr(value, field.name) for field in fields])
    return []


def test_a_stream_ref_keeps_its_type_inside_a_value() -> None:
    converter = temporalio.converter.DataConverter.default.payload_converter
    config = StreamConfig([FaultStore()])
    client = Client(Mock(spec=ServiceClient), stream_config=config)
    # mypy takes a type argument from a variable only on a value of type Any.
    ref_type: Any = StreamRef
    list_type: Any = list
    for seed in range(60):
        rng = random.Random(seed)

        def ref(typed: Any) -> Any:
            store = rng.choice([None, "a", "b"])
            return typed(id=f"stream-{rng.randrange(1000)}", store=store)

        def shape(
            kind: str, depth: int
        ) -> tuple[Any, Callable[[], tuple[Any, list[Any]]]]:
            """Return a hint with a ref in it, and a function that makes a value of
            the hint with the body type of each of its refs, in their order.
            """
            if kind == "argument" or depth == 0:
                body = rng.choice([_Turn, str, int])
                return ref_type[body], lambda: (ref(ref_type[body]), [body])
            if kind == "untyped field":
                # A ref with no type argument reads bodies of any type.
                untyped = dataclasses.make_dataclass("Untyped", [("ref", StreamRef)])
                return untyped, lambda: (untyped(ref(StreamRef)), [object])
            kinds = ["argument", "untyped field", "field", "list", "optional"]
            hint, make = shape(rng.choice(kinds), depth - 1)
            if kind == "optional":
                return hint | None, lambda: make() if rng.random() < 0.7 else (None, [])
            if kind == "list":

                def make_list() -> tuple[Any, list[Any]]:
                    made = [make() for _ in range(rng.randrange(3))]
                    bodies = [body for _, bodies in made for body in bodies]
                    return [value for value, _ in made], bodies

                return list_type[hint], make_list
            holder = dataclasses.make_dataclass("Holder", [("ref", hint)])

            def make_field() -> tuple[Any, list[Any]]:
                value, bodies = make()
                return holder(value), bodies

            return holder, make_field

        # With a depth of 1, these are an argument or a result, a field with no type
        # argument, a field, a list, and an argument or a result that can be None.
        for top in ("argument", "untyped field", "field", "list", "optional"):
            with failure_note(f"seed {seed}, {top}"):
                hint, make = shape(top, rng.randrange(1, 4))
                value, bodies = make()
                payloads = converter.to_payloads([value])
                [decoded] = converter.from_payloads(payloads, [hint])
                assert decoded == value
                refs = _refs(decoded)
                for get_stream in (client.get_stream, temporalio.workflow.get_stream):
                    assert [get_stream(ref).body_type for ref in refs] == bodies


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
    assert len(modules) > 15

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
