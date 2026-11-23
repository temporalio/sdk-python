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
from collections.abc import Callable
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
    """Logs the Store name of each Stream request before and after its call."""

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
        before = input.arg.ref.store
        try:
            return await self.next.start_nexus_operation(input)
        finally:
            self._stores.append((before, input.arg.ref.store))


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
                match rng.choice(["append", "describe", "close"]):
                    case "append":
                        bodies = [rng.randrange(100) for _ in range(rng.randrange(3))]
                        call = stream.append(*bodies)
                        expected = proto.AppendRecordsRequest(
                            stream_id=stream.ref.id,
                            producer=producers[in_activity],
                            records=[
                                AppendRecord(body=to_payload(body)) for body in bodies
                            ],
                        )
                    case "describe":
                        call = stream.describe()
                        expected = proto.DescribeStreamRequest(stream_id=stream.ref.id)
                    case _:
                        call = stream.close()
                        expected = proto.CloseStreamRequest(
                            stream_id=stream.ref.id, producer=producers[in_activity]
                        )
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
                        if wait is not None:
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
            [request] = store.requests[len(logs[stores.index(store)]) :]
            if isinstance(
                request, (proto.CreateStreamRequest, proto.AppendRecordsRequest)
            ):
                assert request.request_id and request.request_id not in request_ids
                request_ids.add(request.request_id)
                request.request_id = ""
            assert request == expected
            assert not service_client.mock_calls
            # An interceptor sees the ref as the caller wrote it, and the Store's name in
            # it after a create.
            after = store.name() if operation == "create" else ref_store
            assert ref_stores == [(ref_store, after)]
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
                [StreamSelectContext(id=stream_id, target=target)]
                if used_selector
                else []
            )
            if operation == "create":
                assert result.ref == StreamRef(id=stream_id, store=store.name())
                created.append((result, store))


@pytest.mark.parametrize(
    "limit", ["max_record_bytes", "max_batch_bytes", "max_batch_records"]
)
def test_a_limit_below_one_raises(limit: str) -> None:
    limits: Any = {limit: 0}
    with pytest.raises(
        ValueError,
        match=rf"^StreamConfig\.{limit} must be greater than or equal to one\.$",
    ):
        StreamConfig([FaultStore()], **limits)


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
        self,
        next: temporalio.client.OutboundInterceptor,
        rng: random.Random,
        records: int,
        closed: bool,
    ) -> None:
        super().__init__(next)
        self._rng = rng
        self._records = records
        self._closed = closed
        self._requests = 0

    async def start_nexus_operation(
        self, input: temporalio.client.StartNexusOperationInput
    ) -> NexusOperationHandle[Any]:
        request: ReadRecordsRequest[int] = input.arg
        # A read that goes on at an end, or that doesn't move on, would never stop.
        self._requests += 1
        assert self._requests <= self._records + 3
        start = int.from_bytes(request.from_offset or b"", "big")
        # A Store answers with no Record only at the end of the Stream.
        size = self._rng.randrange(1, 4)
        end = min(self._records, start + size)
        page = ReadRecordsResponse(
            records=[
                StreamRecord(body=at, offset=at.to_bytes(8, "big"))
                for at in range(start + 1, end + 1)
            ],
            last_offset=end.to_bytes(8, "big") if end else None,
            done=self._closed and end == self._records,
        )
        handle: NexusOperationHandle[Any] = NexusOperationHandle(Mock(), input.id)
        handle._known_outcome = page
        return handle


async def test_a_polled_read_gives_each_record_once_in_order() -> None:
    for seed in range(300):
        with failure_note(f"seed {seed}"):
            await _read_script(random.Random(seed))


async def _read_script(rng: random.Random) -> None:
    records, closed = rng.randrange(12), rng.random() < 0.5
    wait = rng.choice([None, timedelta(0), timedelta(seconds=5)])
    client = Client(
        Mock(spec=ServiceClient),
        interceptors=[
            _Interceptor(lambda next: _FakeReads(next, rng, records, closed))
        ],
    )
    stream = client.get_stream(StreamRef[int](id="s"))
    # A Stream that stays open has no last page for a read that waits, so the loop
    # of such a read stops at a Record.
    waits_forever = bool(wait) and not closed
    stop_at = rng.randrange(1, records + 1) if records else 0
    got: list[int] = []
    first = stream.read(wait=wait)
    if stop_at:
        async for record in first:
            got.append(record.body)
            if len(got) == stop_at:
                break
    assert got == list(range(1, stop_at + 1))
    if waits_forever:
        assert first.next_request is not None
        return
    resumed = first.next_request
    if resumed is not None:
        rest = stream.read(
            from_offset=resumed.from_offset,
            max_records=resumed.max_records,
            wait=resumed.wait,
            max_bytes=resumed.max_bytes,
        )
        got.extend([record.body async for record in rest])
        assert rest.next_request is None
    assert got == list(range(1, records + 1))


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


def _writer_info(rng: random.Random) -> _WriterInfo:
    def text() -> str:
        return "".join(rng.choice("ab:/ '\"é") for _ in range(rng.randrange(1, 5)))

    kinds = ["workflow", "local_activity", "workflow_activity", "standalone_activity"]
    return _WriterInfo(
        kind=rng.choice(kinds),
        namespace=text(),
        workflow_id=text(),
        run_id=text(),
        activity_id=text(),
        attempt=rng.randrange(1, 9),
        time=datetime.fromtimestamp(rng.randrange(10**9), timezone.utc),
        event_id=rng.randrange(3, 99),
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
