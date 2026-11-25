"""Tests of Streams with a Worker on the test server."""

import asyncio
import dataclasses
import hashlib
import itertools
import random
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest
from google.protobuf.message import Message

from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType, StreamErrorReason, TaskQueueType
from temporalio.api.sdk.v1.external_storage_pb2 import ExternalStorageReference
from temporalio.api.streamservice.v1 import (
    AppendRecordsRequest,
    CreateStreamRequest,
    DescribeStreamResponse,
    ReadRecordsRequest,
)
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client, StreamConfig, WorkflowFailureError
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
)
from temporalio.exceptions import ApplicationError, StreamError
from temporalio.nexus.system.stream_service import StreamIdConflictPolicy, StreamRef
from temporalio.testing import MemoryStreamStore
from temporalio.worker import (
    ActivityInboundInterceptor,
    Interceptor,
    Replayer,
    Worker,
)
from tests.helpers import assert_eventually, assert_task_fail_eventually
from tests.helpers.streams import (
    ContextCodec,
    FaultStore,
    Hold,
    RunOptions,
    answers,
    failure_note,
    loses_answer,
    raises,
    run,
    stream_client,
)
from tests.test_extstore import InMemoryTestDriver


async def _answers(
    api: Any, calls: Sequence[Sequence[Any]], store: str | None = None
) -> list[Any]:
    """Make each generated Stream call in turn and return what each one gave.

    ``api`` is a client or the ``temporalio.workflow`` module, which both have
    ``create_stream`` and ``get_stream``. ``store`` is the Store name of each ref.
    """
    streams: dict[str, Any] = {}
    answers: list[Any] = []
    for operation, stream_id, *args in calls:
        stream = streams.get(stream_id) or api.get_stream(
            StreamRef[int](id=stream_id, store=store)
        )
        try:
            if operation == "create":
                policy = StreamIdConflictPolicy(args[0]) if args and args[0] else None
                stream = await api.create_stream(
                    id=stream_id, store=store, id_conflict_policy=policy, body_type=int
                )
                streams[stream_id] = stream
                answers.append([stream.ref.store, stream.created])
            elif operation == "append":
                appended = await stream.append(*args[0])
                answers.append(
                    [appended.first_offset.hex(), appended.last_offset.hex()]
                )
            elif operation == "read":
                page = await stream.read_page()
                bodies = [record.body for record in page.records]
                answers.append([bodies, (page.last_offset or b"").hex()])
            else:
                described = await stream.describe()
                answers.append([(described.last_offset or b"").hex()])
        except StreamError as err:
            answers.append(["error", err.reason, err.message])
    return answers


@activity.defn
async def _calls_activity(calls: list[list[Any]]) -> list[Any]:
    return await _answers(activity.client(), calls)


@workflow.defn
class _CallsWorkflow:
    @workflow.run
    async def run(self, calls: list[list[Any]], in_activity: bool = False) -> list[Any]:
        if in_activity:
            return await workflow.execute_activity(
                _calls_activity, calls, start_to_close_timeout=timedelta(seconds=10)
            )
        return await _answers(workflow, calls)


def _writes(store: FaultStore) -> list[Message]:
    """Return each request of a Store but its reads, which Core pages in its own way,
    without the Producer, which differs between two writers of the same call.
    """
    writes: list[Message] = []
    for request in store.requests:
        if isinstance(request, ReadRecordsRequest):
            continue
        copy = type(request)()
        copy.CopyFrom(request)
        if "producer" in copy.DESCRIPTOR.fields_by_name:
            copy.ClearField("producer")
        writes.append(copy)
    return writes


async def test_workflow_code_and_a_client_get_the_same_answers(client: Client) -> None:
    for seed in range(8):
        with failure_note(f"seed {seed}"):
            rng = random.Random(seed)
            # One run sends one request id with each of its creates, so a second create
            # of a Stream would be the first one again for a Workflow and not for a client.
            calls: list[list[Any]] = [
                ["create", f"s-{seed}-{number}", rng.randrange(3)] for number in (0, 1)
            ]
            operations = ["append", "append", "append", "describe"]
            reads = seed % 2 == 1
            if reads:
                operations += ["read", "read"]
            for _ in range(rng.randrange(4, 12)):
                # The third Stream is never created.
                stream_id = f"s-{seed}-{rng.choice([0, 0, 0, 1, 1, 2])}"
                operation = rng.choice(operations)
                if operation == "append":
                    bodies = [rng.randrange(100) for _ in range(rng.randrange(1, 4))]
                    calls.append(["append", stream_id, bodies])
                else:
                    calls.append([operation, stream_id])
            if reads:
                calls += [["read", f"s-{seed}-0"], ["read", f"s-{seed}-1"]]
            # The two Stores have one name and the same Stream ids, so a call is the same
            # request in both.
            for_client, for_workflow = FaultStore(), FaultStore()
            from_client = await _answers(stream_client(client, for_client), calls)
            workflow_client = stream_client(client, for_workflow)
            ran = await run(workflow_client, RunOptions(_CallsWorkflow, [calls]))
            assert ran.result == from_client
            assert _writes(for_workflow) == _writes(for_client)
            appends = [
                request_id
                for request, request_id in for_workflow.calls
                if isinstance(request, AppendRecordsRequest)
            ]
            assert all(appends) and len(set(appends)) == len(appends)
            # A replay with the Stores of the run writes nothing. A replay chooses the
            # Store of each call again, so one with no Stream config fails.
            written = _writes(for_workflow)
            await Replayer(
                workflows=[_CallsWorkflow],
                stream_config=workflow_client._config["stream_config"],
            ).replay_workflow(ran.history)
            assert _writes(for_workflow) == written
            with pytest.raises(Exception, match="no stream_config"):
                await Replayer(workflows=[_CallsWorkflow]).replay_workflow(ran.history)


def _bodies(writer: str) -> list[Any]:
    """Return the bodies that a writer appends. One is a value that a pass of external
    storage would take for a reference into the storage of the Worker.
    """
    claim = ExternalStorageReference(
        driver_name="test-driver", claim_data={"key": "payload-0"}
    )
    return [f"{writer} plain", {"writer": writer}, claim]


def _digest(bodies: Sequence[Any]) -> str:
    """Return a digest of bodies, so that a Workflow result has no body in it."""
    return hashlib.sha256(repr(bodies).encode()).hexdigest()


@activity.defn
async def _append_bodies(stream_id: str) -> None:
    stream = activity.client().get_stream(StreamRef(id=stream_id))
    await stream.append(*_bodies("activity"))


@workflow.defn
class _BodiesWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        await workflow.execute_activity(
            _append_bodies, stream_id, start_to_close_timeout=timedelta(seconds=10)
        )
        stream = workflow.get_stream(StreamRef[Any](id=stream_id))
        await stream.append(*_bodies("workflow"))
        return _digest([record.body async for record in stream.read()])


async def test_a_body_reads_the_same_on_every_path(client: Client) -> None:
    store, driver = FaultStore(), InMemoryTestDriver()
    converter = dataclasses.replace(
        DataConverter.default,
        payload_codec=ContextCodec(),
        external_storage=ExternalStorage(drivers=[driver], payload_size_threshold=0),
    )
    client = stream_client(client, store, data_converter=converter)
    stream = await client.create_stream(id="bodies")
    await stream.append(*_bodies("client"))
    options = RunOptions(_BodiesWorkflow, ["bodies"], activities=[_append_bodies])
    ran = await run(client, options)

    expected = [*_bodies("client"), *_bodies("activity"), *_bodies("workflow")]
    assert [record.body async for record in stream.read()] == expected
    assert ran.result == _digest(expected)
    # The Store has each body as the codec gave it with no context, and the storage of
    # the Worker has none of them.
    stored = await store.memory.read_records(ReadRecordsRequest(stream_id="bodies"))
    assert [dict(record.body.metadata) for record in stored.records] == [
        {"encoding": b"test/wrapped", "context": b"NoneType"}
    ] * len(expected)
    assert driver._storage and not any(
        b" plain" in value for value in driver._storage.values()
    )


@activity.defn
async def _append_one(stream_id: str) -> None:
    await activity.client().get_stream(StreamRef[int](id=stream_id)).append(1)


@workflow.defn
class _OneActivityIdWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        for _ in range(2):
            await workflow.execute_activity(
                _append_one,
                stream_id,
                activity_id="one id",
                start_to_close_timeout=timedelta(seconds=10),
            )


async def test_two_activities_with_one_id_write_as_two_producers(
    client: Client,
) -> None:
    store = FaultStore()
    client = stream_client(client, store)
    await client.create_stream(id="one id", body_type=int)
    options = RunOptions(_OneActivityIdWorkflow, ["one id"], activities=[_append_one])
    ran = await run(client, options)
    # The Producer of each Activity has the id of the Activity's scheduled event.
    scheduled = _events(ran.history, EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    page = await store.memory.read_records(ReadRecordsRequest(stream_id="one id"))
    producers = [record.producer.workflow_activity for record in page.records]
    assert [(p.activity_id, p.scheduled_event_id) for p in producers] == [
        ("one id", event.event_id) for event in scheduled
    ]


def _events(history: Any, event_type: EventType.ValueType) -> list[Any]:
    return [event for event in history.events if event.event_type == event_type]


def _stream_markers(history: Any, kind: str) -> list[Any]:
    """Return the markers in which Core records the Stream calls of ``kind``, which
    is ``append`` or ``read``.
    """
    return [
        event
        for event in _events(history, EventType.EVENT_TYPE_MARKER_RECORDED)
        if event.marker_recorded_event_attributes.marker_name == f"core_stream_{kind}"
    ]


async def test_a_store_error_is_the_same_error_on_every_path(client: Client) -> None:
    store = FaultStore()
    client = stream_client(client, store)
    await client.create_stream(id="errors", body_type=int)
    append: list[Any] = ["append", "errors", [1]]
    appends = store.faults["append_records"]
    unavailable = StreamErrorReason.STREAM_ERROR_REASON_STORE_UNAVAILABLE

    async def answers_on(
        path: str, calls: list[list[Any]], on: Client = client, **options: Any
    ) -> Any:
        if path == "client":
            return await _answers(on, calls)
        args = [calls, path == "activity"]
        run_options = RunOptions(
            _CallsWorkflow, args, activities=[_calls_activity], **options
        )
        return (await run(on, run_options)).result

    def append_request_ids() -> list[str | None]:
        calls, store.calls = store.calls, []
        return [i for r, i in calls if isinstance(r, AppendRecordsRequest)]

    # Core tries an append again in an outage of the Store, with one request id, so the
    # Store writes it once. Workflow code gets no error, and History has no failure.
    outages = [
        [raises(StreamError("The Store is down", reason=unavailable))] * 2,
        [loses_answer()],
        [raises(ConnectionError("The connection is gone"))],
    ]
    for at, faults in enumerate(outages):
        with failure_note(f"outage {at}"):
            appends.extend(faults)
            ran = await run(client, RunOptions(_CallsWorkflow, [[append]]))
            offset = (at + 1).to_bytes(8, "big").hex()
            assert ran.result == [[offset, offset]]
            request_ids = append_request_ids()
            assert len(request_ids) == len(faults) + 1
            assert len(set(request_ids)) == 1
            assert not _events(ran.history, EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED)
    # A client and an Activity get the outage as the error of their call.
    for path in ("client", "activity"):
        appends.append(raises(ConnectionError("The connection is gone")))
        [[error, reason, _]] = await answers_on(path, [append])
        assert (error, reason, len(append_request_ids())) == ("error", unavailable, 1)

    # An answer of the wrong message fails the call, and nothing tries it again.
    corrupt = StreamErrorReason.STREAM_ERROR_REASON_STORE_CORRUPT
    for path in ("client", "activity", "workflow"):
        with failure_note(path):
            appends.append(answers(DescribeStreamResponse()))
            [[error, reason, _]] = await answers_on(path, [append])
            assert (error, reason, len(append_request_ids())) == ("error", corrupt, 1)

    # A refusal is the same error for a client, an Activity and Workflow code. Workflow
    # code gets it again in each task that replays the call.
    not_found = StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND
    calls: list[list[Any]] = [
        ["append", "missing", [1]],
        ["describe", "errors"],
        ["describe", "missing"],
        ["read", "missing"],
    ]
    refused = await answers_on("client", calls)
    assert [answer[:2] for answer in refused] == [
        ["error", not_found],
        [(3).to_bytes(8, "big").hex()],
        ["error", not_found],
        ["error", not_found],
    ]
    for path in ("activity", "workflow"):
        with failure_note(path):
            assert await answers_on(path, calls, cached=False, replay=True) == refused

    # When its Retention has passed since its last append, a Stream refuses each call
    # as expired, on each path.
    expiring = stream_client(client, FaultStore(retention=timedelta(seconds=2)))
    stream = await expiring.create_stream(id="expires", body_type=int)
    await stream.append(1)
    await asyncio.sleep(2.5)
    expired = StreamErrorReason.STREAM_ERROR_REASON_EXPIRED
    calls = [["append", "expires", [2]], ["describe", "expires"], ["read", "expires"]]
    for path in ("client", "activity", "workflow"):
        with failure_note(f"expired, {path}"):
            answered = await answers_on(path, calls, on=expiring, replay=True)
            assert [answer[:2] for answer in answered] == [["error", expired]] * 3

    # An outage during a create fails the Workflow Task, also on a Worker that fails a
    # Workflow for any exception. The create runs in the next task, with the Store back.
    for failure_types in ([], [Exception]):
        with failure_note(f"failure types {failure_types}"):
            store.faults["create_stream"].append(raises(ConnectionError("down")))
            options = RunOptions(
                _CallsWorkflow,
                [[["create", f"after an outage {len(failure_types)}"]]],
                worker={"workflow_failure_exception_types": failure_types},
            )
            ran = await run(client, options)
            assert ran.result == [["memory", True]]
            # History has nothing of the outage but the task failure.
            events = [event.event_type for event in ran.history.events]
            task_done = events.index(EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED)
            assert set(events[1:task_done]) == {
                EventType.EVENT_TYPE_WORKFLOW_TASK_SCHEDULED,
                EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED,
                EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED,
            }
            assert events[task_done + 1 :] == [
                EventType.EVENT_TYPE_MARKER_RECORDED,
                EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
            ]


class _DownReadsStore(FaultStore):
    """A Store whose reads fail as an outage until :py:attr:`up` is true."""

    def __init__(self) -> None:
        super().__init__()
        self.up = False

    async def read_records(self, request: ReadRecordsRequest) -> Any:
        if not self.up:
            self.calls.append((request, None))
            raise ConnectionError("The connection is gone")
        return await super().read_records(request)


async def test_a_store_outage_at_a_workflow_read_fails_the_workflow_task(
    client: Client,
) -> None:
    store = _DownReadsStore()
    client = stream_client(client, store)
    await (await client.create_stream(id="down", body_type=int)).append(1)

    async def back_after_a_task_failure(handle: Any, _worker: Any) -> None:
        await assert_task_fail_eventually(handle)
        store.up = True

    # SDK Core tries the read again up to the read limit, and then fails the task, as
    # for an append. Workflow code gets no error, also on a Worker that fails a
    # Workflow for any exception, and the read runs in the next task.
    for failure_types in ([], [Exception]):
        with failure_note(f"failure types {failure_types}"):
            store.up = False
            options = RunOptions(
                _CallsWorkflow,
                [[["read", "down"]]],
                task_timeout=timedelta(seconds=2),
                worker={"workflow_failure_exception_types": failure_types},
                during=back_after_a_task_failure,
            )
            ran = await run(client, options)
            assert ran.result == [[[1], (1).to_bytes(8, "big").hex()]]
            failed = _events(ran.history, EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED)
            assert failed
            for event in failed:
                message = event.workflow_task_failed_event_attributes.failure.message
                assert message.startswith("StreamStoreUnavailable:"), message


@workflow.defn
class _CatchingWorkflow:
    @workflow.run
    async def run(self, call: list[Any], store: str | None) -> str:
        try:
            await _answers(workflow, [call], store)
        except Exception:
            pass
        return "caught"


async def test_a_stream_call_with_no_store_fails_the_workflow_task(
    client: Client,
) -> None:
    async def task_fails(handle: Any, _worker: Any) -> None:
        await assert_task_fail_eventually(handle)
        await handle.terminate()

    cases = {
        "no Stream config": (client, None),
        "a ref of a Store that the Worker lacks": (
            stream_client(client, FaultStore()),
            "other",
        ),
    }
    calls: list[list[Any]] = [
        ["create", "s"],
        ["append", "s", [1]],
        ["describe", "s"],
        ["read", "s"],
    ]
    # The task fails also on a Worker that fails a Workflow for any exception.
    for (case, (worker_client, store)), call, failure_types in itertools.product(
        cases.items(), calls, ([], [Exception])
    ):
        with failure_note(f"{call[0]} with {case} and {failure_types}"):
            options = RunOptions(
                _CatchingWorkflow,
                [call, store],
                worker={"workflow_failure_exception_types": failure_types},
                during=task_fails,
            )
            ran = await run(worker_client, options)
            # The Workflow didn't go on after the error that it caught, and History
            # has nothing of the call.
            assert isinstance(ran.result, WorkflowFailureError)
            assert not _events(ran.history, EventType.EVENT_TYPE_MARKER_RECORDED)


class _ActivityTypes(Interceptor):
    """Logs the type of each Activity that its Worker runs."""

    def __init__(self) -> None:
        super().__init__()
        self.types: list[str] = []

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        self.types.append(activity.info().activity_type)
        return next


async def test_a_worker_with_only_workflows_creates_a_stream(client: Client) -> None:
    client = stream_client(client, FaultStore())
    interceptor = _ActivityTypes()
    pollers: list[Any] = []

    async def activity_pollers(handle: Any, worker: Worker) -> None:
        await handle.result()
        described = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=client.namespace,
                task_queue=TaskQueue(name=worker.task_queue),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
            )
        )
        pollers.extend(described.pollers)

    options = RunOptions(
        _CallsWorkflow,
        [[["create", "alone"], ["describe", "alone"]]],
        worker={"interceptors": [interceptor]},
        during=activity_pollers,
    )
    ran = await run(client, options)
    assert ran.result == [["memory", True], [""]]
    assert interceptor.types == ["__temporal_stream_store_call"] * 2
    assert not pollers


@activity.defn
async def _append_to(stream_id: str) -> None:
    stream = activity.client().get_stream(StreamRef[int](id=stream_id))
    await stream.append(3)


@workflow.defn
class _SwapWorkflow:
    def __init__(self) -> None:
        self._swapped = False

    @workflow.signal
    def swapped(self) -> None:
        self._swapped = True

    @workflow.run
    async def run(self, own: str, shared: str) -> str | None:
        created = await workflow.create_stream(id=own, body_type=int)
        await workflow.wait_condition(lambda: self._swapped)
        await created.append(1)
        stream = workflow.get_stream(StreamRef[int](id=shared))
        await stream.append(2)
        await workflow.execute_activity(
            _append_to, shared, start_to_close_timeout=timedelta(seconds=10)
        )
        return created.ref.store


def _calls(store: FaultStore) -> list[tuple[str, str]]:
    """Return the request type and the Stream id of each call of a Store."""
    return [(type(r).__name__, r.stream_id) for r in store.requests]


# On a server with worker commands, a lone Worker polls in a loop after a new client. Marked as
# `test_workflow_replace_worker_client` and `test_nexus_client_updates_when_worker_client_changes`.
@pytest.mark.requires_local_server
async def test_a_worker_uses_the_stream_config_of_its_new_client(
    client: Client,
) -> None:
    for cached in (True, False):
        with failure_note(f"cached {cached}"):
            first, second = FaultStore("first"), FaultStore("second")
            # The new client has other Store objects, with the Streams of the first ones.
            new_first, new_second = FaultStore("first"), FaultStore("second")
            new_first.memory = first.memory
            selects: list[tuple[str, str | None]] = []

            def pick_first(context: Any) -> FaultStore:
                selects.append((type(context.target).__name__, context.namespace))
                return first

            before = stream_client(client, first, second, store_selector=pick_first)
            after = stream_client(
                client, new_first, new_second, store_selector=lambda _: new_second
            )
            await after.create_stream(id="shared", body_type=int)

            async def swap(handle: Any, worker: Worker) -> None:
                async def created() -> None:
                    assert first.requests

                await assert_eventually(created)
                # A client with other Store names or other limits is refused.
                for other in (
                    stream_client(client, FaultStore("third")),
                    stream_client(
                        client,
                        first,
                        second,
                        store_selector=pick_first,
                        max_batch_records=1,
                    ),
                ):
                    with pytest.raises(ValueError):
                        worker.client = other
                worker.client = after
                await handle.signal(_SwapWorkflow.swapped)

            options = RunOptions(
                _SwapWorkflow,
                ["own", "shared"],
                activities=[_append_to],
                cached=cached,
                during=swap,
            )
            # The created Stream stays on its Store. A ref with no Store name goes to the
            # Store that the new selector picks, in Workflow code as in an Activity.
            assert (await run(before, options)).result == "first"
            assert _calls(first) == [("CreateStreamRequest", "own")]
            # After the swap, Core calls the Store objects of the new client.
            assert _calls(new_first) == [("AppendRecordsRequest", "own")]
            assert _calls(new_second) == [
                ("CreateStreamRequest", "shared"),
                ("AppendRecordsRequest", "shared"),
                ("AppendRecordsRequest", "shared"),
            ]
            assert not second.requests
            # The selector of the Workflow's create got the Workflow as its target, and the
            # namespace of the Worker.
            assert selects == [("StorageDriverWorkflowInfo", client.namespace)]


async def test_an_append_over_a_limit_of_the_config_never_reaches_the_store(
    client: Client,
) -> None:
    too_large = StreamErrorReason.STREAM_ERROR_REASON_TOO_LARGE
    unsupported = StreamErrorReason.STREAM_ERROR_REASON_UNSUPPORTED
    default_bytes = 4 * 1024 * 1024
    for seed in range(12):
        with failure_note(f"seed {seed}"):
            rng = random.Random(seed)
            # One seed in four has limits over the defaults, which only the config bounds.
            larger = seed % 4 == 0
            record = rng.choice([1000, 3000, 1536 * 1024])
            batch = (
                default_bytes if larger else rng.choice([4000, 20_000, default_bytes])
            )
            count = 1200 if larger else rng.choice([1, 3, 1000])
            # The body sizes of each batch, and whether the batch is within every limit.
            # A stored Record counts its Producer, so no size is within 500 bytes of a
            # limit, and the test needs no size logic.
            batches: list[tuple[list[int], bool]] = []
            if larger:
                batches.append(([0] * 1100, True))
            for _ in range(6):
                match rng.choice(["fits", "fits", "records", "record", "batch"]):
                    case "records":
                        batches.append(([0] * (count + 1), False))
                    case "record" if record <= 3000:
                        batches.append(([2 * record], False))
                    case "batch" if batch <= 20_000:
                        size = min(record, batch) - 500
                        batches.append(([size] * (batch // size + 1), False))
                    case _:
                        fits = [rng.randrange(40)] * rng.randrange(1, min(count, 3) + 1)
                        batches.append((fits, True))
            store = FaultStore()
            streams = stream_client(
                client,
                store,
                max_record_bytes=record,
                max_batch_bytes=batch,
                max_batch_records=count,
            )

            def calls(stream_id: str) -> list[list[Any]]:
                appends = [["x" * size for size in sizes] for sizes, _ in batches]
                return [["create", stream_id]] + [
                    ["append", stream_id, bodies] for bodies in appends
                ]

            from_client = await _answers(streams, calls(f"client {seed}"))
            workflow_calls = calls(f"workflow {seed}")
            if record <= 3000:
                # A client with a larger limit writes a Record over the limit of the
                # Worker, and a read of Workflow code refuses that Record.
                writer = stream_client(client, store)
                large = await writer.create_stream(id=f"large {seed}", body_type=str)
                await large.append("x" * 2 * record)
                workflow_calls.append(["read", f"large {seed}"])
            options = RunOptions(_CallsWorkflow, [workflow_calls], replay=True)
            from_workflow = (await run(streams, options)).result
            if record <= 3000:
                assert from_workflow.pop()[:2] == ["error", unsupported]
            for answers in (from_client, from_workflow):
                assert [
                    "written" if answer[0] != "error" else answer[1]
                    for answer in answers[1:]
                ] == ["written" if fits else too_large for _, fits in batches]
            # The Store got the batches within the limits from each path, and no other.
            assert [
                len(request.records)
                for request in store.requests
                if isinstance(request, AppendRecordsRequest)
                and request.stream_id != f"large {seed}"
            ] == [len(sizes) for sizes, fits in batches if fits] * 2


@workflow.defn
class _ChainWorkflow:
    @workflow.run
    async def run(self, stream_id: str, runs: int) -> bool | None:
        stream = await workflow.create_stream(id=stream_id)
        if runs > 1:
            workflow.continue_as_new(args=[stream_id, runs - 1])
        return stream.created


async def test_a_create_in_a_later_run_of_the_chain_is_the_same_create(
    client: Client,
) -> None:
    store = FaultStore()
    # The Store loses the answer of the first create of each run, so the Workflow Task
    # fails after its create ran, and its next attempt makes the create again.
    faults = [loses_answer(), lambda call: call(), loses_answer()]
    store.faults["create_stream"].extend(faults)
    ran = await run(
        stream_client(client, store), RunOptions(_ChainWorkflow, ["chain", 2])
    )
    # Each run makes its create twice, and the Store takes all four for one create.
    assert ran.result is True
    creates = [i for r, i in store.calls if isinstance(r, CreateStreamRequest)]
    assert len(creates) == 4 and len(set(creates)) == 1


_handled: dict[int, asyncio.Event] = {}
"""An event for each Record of the writing Activity, set once the Workflow handled it."""


@activity.defn
async def _write_one_by_one(stream_id: str, count: int) -> None:
    client = activity.client()
    stream = client.get_stream(StreamRef[str](id=stream_id))
    workflow_id = activity.info().workflow_id or ""
    for number in range(count):
        await stream.append(f"a body, number {number}")
        # A read makes no Workflow Task for a Record that SDK Core doesn't know of, so
        # the writer signals the Workflow.
        await client.get_workflow_handle(workflow_id).signal(_ReadingWorkflow.wake)
        # The next Record comes only once the Workflow has handled this one.
        await _handled[number].wait()


@activity.defn
async def _handle(number: int) -> None:
    _handled[number].set()


@workflow.defn
class _ReadingWorkflow:
    @workflow.signal
    def wake(self) -> None:
        pass

    @workflow.run
    async def run(self, stream_id: str, count: int) -> list[int]:
        stream = await workflow.create_stream(id=stream_id, body_type=str)
        writer = workflow.start_activity(
            _write_one_by_one,
            args=[stream_id, count],
            start_to_close_timeout=timedelta(seconds=30),
        )
        numbers: list[int] = []
        async for record in stream.read(wait=timedelta(seconds=30)):
            number = int(record.body.rsplit(" ", 1)[1])
            await workflow.execute_activity(
                _handle, number, start_to_close_timeout=timedelta(seconds=30)
            )
            numbers.append(number)
            # A Stream has no end, so the reader stops at the writer's last Record.
            if number == count - 1:
                break
        await writer
        return numbers


async def test_a_workflow_reads_the_records_of_an_activity_that_still_runs(
    client: Client,
) -> None:
    count = 5
    for cached in (True, False):
        with failure_note(f"cached {cached}"):
            _handled.clear()
            _handled.update({number: asyncio.Event() for number in range(count)})
            options = RunOptions(
                _ReadingWorkflow,
                [f"read while written, cached {cached}", count],
                activities=[_write_one_by_one, _handle],
                cached=cached,
                replay=True,
            )
            ran = await run(stream_client(client, FaultStore()), options)
            assert ran.result == list(range(count))
            assert not any(
                b"a body" in event.SerializeToString() for event in ran.history.events
            )


@workflow.defn
class _EndingWorkflow:
    """Starts a Stream call that nothing awaits, and ends in the way of ``how``."""

    def __init__(self) -> None:
        self._signals = 0

    @workflow.signal
    def go_on(self) -> None:
        self._signals += 1

    @workflow.run
    async def run(self, stream_id: str, how: str) -> None:
        stream = await workflow.create_stream(id=stream_id, body_type=int)
        if how == "append":
            asyncio.ensure_future(stream.append(1))
        elif how == "page":
            await stream.append(1)
            await stream.read_page(wait=timedelta(minutes=5))
            return
        else:
            asyncio.ensure_future(stream.read_page(wait=timedelta(minutes=5)))
            await workflow.wait_condition(lambda: self._signals > 0)
        # The task of the call makes its call in its first step.
        await asyncio.sleep(0)


async def test_a_read_that_core_stops_leaves_no_store_task(client: Client) -> None:
    # The read limit of the task stops a held Store call, also while the Worker shuts
    # down, since the shutdown waits for the task.
    for how in ("read limit", "shutdown"):
        with failure_note(how):
            store, hold = FaultStore(), Hold()
            store.faults["read_records"].append(hold)

            async def end(handle: Any, worker: Worker) -> None:
                await hold.started.wait()
                if how == "shutdown":
                    await worker.shutdown()
                else:
                    await handle.signal(_EndingWorkflow.go_on)
                await asyncio.wait_for(hold.cancelled.wait(), 10)
                if how == "shutdown":
                    await handle.terminate()

            # A task timeout of 2 seconds gives a read limit of 1 second.
            options = RunOptions(
                _EndingWorkflow,
                [f"ends by {how}", how],
                task_timeout=timedelta(seconds=2),
                during=end,
                replay=True,
            )
            ran = await run(stream_client(client, store), options)
            assert (ran.result is None) == (how == "read limit")
            reads = [r for r in store.requests if isinstance(r, ReadRecordsRequest)]
            assert len(reads) == 1


@pytest.mark.parametrize("how", ["page", "append"])
async def test_a_stream_call_does_not_hold_the_last_command(
    client: Client, how: str
) -> None:
    # A read that gave a page, and an append that nothing awaits, don't hold
    # the last command, so the Workflow ends in the task of the call.
    options = RunOptions(_EndingWorkflow, [f"ends after its {how}", how], replay=True)
    ran = await run(stream_client(client, FaultStore()), options)
    assert ran.result is None
    assert len(_events(ran.history, EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED)) == 1


@workflow.defn
class _LatePagesWorkflow:
    """Reads Records that come in a later Workflow Task than the one of its reads."""

    @workflow.signal
    def wake(self) -> None:
        pass

    @workflow.run
    async def run(self, how: str) -> list[list[str]]:
        s = workflow.get_stream(StreamRef[str](id=f"s of {how}"))
        t = workflow.get_stream(StreamRef[str](id=f"t of {how}"))
        if how == "two readers":
            # One reader takes a page of `s` and stops. The other waits for a page of
            # `t`, and then reads `s` on from the offset of the first one.
            first = asyncio.ensure_future(self._page(s))
            await self._page(t)
            offset = (await first).last_offset
            second = await self._page(s, offset)
            return [[r.body for r in page.records] for page in (await first, second)]
        pages: list[list[str]] = []
        offset = None
        while True:
            page = await s.read_page(from_offset=offset, wait=timedelta(seconds=30))
            pages.append([record.body for record in page.records])
            offset = page.last_offset
            # A Stream has no end, so the loop stops at the writer's last Record.
            if "b" in pages[-1]:
                return pages

    async def _page(self, stream: Any, offset: bytes | None = None) -> Any:
        """Return the first page of ``stream`` after ``offset`` that has a Record."""
        while True:
            # At the read limit of its task, a read gives a page with no Record.
            page = await stream.read_page(
                from_offset=offset, wait=timedelta(seconds=30)
            )
            if page.records:
                return page
            offset = page.last_offset


@pytest.mark.parametrize("how", ["a loop", "two readers"])
async def test_a_replay_gives_a_read_the_pages_of_the_first_run(
    client: Client, how: str
) -> None:
    store = FaultStore()
    client = stream_client(client, store)
    s = await client.create_stream(id=f"s of {how}", body_type=str)
    t = await client.create_stream(id=f"t of {how}", body_type=str)
    writes, expected = {
        "a loop": ([(s, "a"), (s, "b")], ["a", "b"]),
        "two readers": ([(s, "a"), (t, "x"), (s, "b")], ["a", "b"]),
    }[how]

    async def write(handle: Any, _worker: Any) -> None:
        async def first_task() -> None:
            history = await handle.fetch_history()
            completed = EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
            assert _events(history, completed)

        # The reads of the first task end with no Record, and a read of an idle Stream
        # makes no task. A Signal opens the next task, whose reads start again. The
        # pages then come in several activations of that task, and a replay gives each
        # page in its activation.
        await assert_eventually(first_task)
        await handle.signal(_LatePagesWorkflow.wake)
        for stream, body in writes:
            await stream.append(body)
            # The page of this Record gets an activation before the next Record comes.
            await asyncio.sleep(0.3)

    # The default read limit is half the task timeout, so a read with no Record
    # is cut at 3 s, and a slow host still doesn't time out the task.
    options = RunOptions(
        _LatePagesWorkflow,
        [how],
        task_timeout=timedelta(seconds=6),
        during=write,
        replay=True,
    )
    ran = await run(client, options)
    assert [body for page in ran.result for body in page] == expected
    events = [event.event_type for event in ran.history.events]
    completed = events.index(EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED)
    marker = EventType.EVENT_TYPE_MARKER_RECORDED
    later = [event for event in events[completed + 1 :] if event != marker]
    assert later[0] == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED


@workflow.defn
class _TwoReadsWorkflow:
    @workflow.run
    async def run(self, first: str, second: str) -> list[list[str]]:
        wait = timedelta(seconds=30)
        streams = [workflow.get_stream(StreamRef[str](id=id)) for id in (first, second)]
        pages = await asyncio.gather(
            *(stream.read_page(wait=wait) for stream in streams)
        )
        return [[record.body for record in page.records] for page in pages]


async def test_a_replay_gives_each_result_in_the_activation_of_the_first_run(
    client: Client,
) -> None:
    # A local import, so that the sandbox of each Workflow here doesn't import that module.
    from tests.worker.test_workflow import CustomWorkflowRunner

    store, holds = FaultStore(), [Hold(), Hold()]
    store.faults["read_records"].extend(holds)
    client = stream_client(client, store)
    for stream_id in ("first", "second"):
        stream = await client.create_stream(id=stream_id, body_type=str)
        await stream.append(stream_id)
    live, replayed = CustomWorkflowRunner(), CustomWorkflowRunner()

    def read_results(runner: CustomWorkflowRunner) -> list[list[int]]:
        """Return the seqs of the read results of each activation that has one."""
        activations = [
            [
                job.resolve_stream_read.seq
                for job in activation.jobs
                if job.HasField("resolve_stream_read")
            ]
            for activation, _ in runner._pairs
        ]
        return [seqs for seqs in activations if seqs]

    async def release(_handle: Any, _worker: Any) -> None:
        # Both reads wait in the first task, and each page comes before its read limit.
        for hold in holds:
            await hold.started.wait()
        for count, hold in enumerate(holds, 1):
            hold.release()

            async def given() -> None:
                assert len(read_results(live)) == count

            # This page gets its activation before the Store answers the other read.
            await assert_eventually(given)

    options = RunOptions(
        _TwoReadsWorkflow,
        ["first", "second"],
        worker={"workflow_runner": live},
        during=release,
    )
    ran = await run(client, options)
    assert ran.result == [["first"], ["second"]]
    await Replayer(
        workflows=[_TwoReadsWorkflow],
        workflow_runner=replayed,
        stream_config=client._config["stream_config"],
    ).replay_workflow(ran.history)
    assert len(read_results(live)) == 2
    assert read_results(replayed) == read_results(live)


@workflow.defn
class _ReadOnceWorkflow:
    def __init__(self) -> None:
        self._go_on = False

    @workflow.signal
    def go_on(self) -> None:
        self._go_on = True

    @workflow.run
    async def run(self, stream_id: str) -> list[int]:
        stream = workflow.get_stream(StreamRef[int](id=stream_id))
        bodies = [record.body async for record in stream.read()]
        await workflow.wait_condition(lambda: self._go_on)
        return bodies


@workflow.defn(failure_exception_types=[workflow.StreamReplayError])
class _ReadOnceOptedInWorkflow(_ReadOnceWorkflow):
    @workflow.run
    async def run(self, stream_id: str) -> list[int]:
        return await super().run(stream_id)


async def test_a_replay_reads_its_python_stores(client: Client) -> None:
    store = FaultStore()
    stream = await stream_client(client, store).create_stream(
        id="replayed", body_type=int
    )
    await stream.append(1, 2, 3)

    async def go_on(handle: Any, _worker: Any) -> None:
        await handle.signal(_ReadOnceWorkflow.go_on)

    options = RunOptions(_ReadOnceWorkflow, ["replayed"], during=go_on)
    ran = await run(stream_client(client, store), options)
    assert ran.result == [1, 2, 3]
    # A `Replayer` reads the Records again from the Store, and fails without them.
    replayer = Replayer(
        workflows=[_ReadOnceWorkflow], stream_config=StreamConfig([store])
    )
    reads = len(store.requests)
    await replayer.replay_workflow(ran.history)
    assert isinstance(store.requests[reads], ReadRecordsRequest)
    store.memory = MemoryStreamStore()
    with pytest.raises(RuntimeError):
        await replayer.replay_workflow(ran.history)

    # A Worker with no cache replays the read in its second task. With the Records gone
    # the task fails, or the Workflow with the opt-in of the Worker or of the Workflow.
    cases: dict[str, tuple[type, list[type[BaseException]]]] = {
        "no opt-in": (_ReadOnceWorkflow, []),
        "the Worker option": (_ReadOnceWorkflow, [workflow.StreamReplayError]),
        "the Workflow option": (_ReadOnceOptedInWorkflow, []),
    }
    for case, (workflow_class, failure_types) in cases.items():
        with failure_note(case):
            store = FaultStore()
            stream_id = f"gone with {case}"
            stream = await stream_client(client, store).create_stream(
                id=stream_id, body_type=int
            )
            await stream.append(1, 2, 3)

            async def lose_the_records(handle: Any, _worker: Any) -> None:
                async def has_read() -> None:
                    history = await handle.fetch_history()
                    assert _stream_markers(history, "read")

                await assert_eventually(has_read)
                store.memory = MemoryStreamStore()
                await handle.signal(_ReadOnceWorkflow.go_on)
                if case == "no opt-in":
                    await assert_task_fail_eventually(handle)
                    await handle.terminate()

            options = RunOptions(
                workflow_class,
                [stream_id],
                cached=False,
                worker={"workflow_failure_exception_types": failure_types},
                during=lose_the_records,
            )
            ran = await run(stream_client(client, store), options)
            assert isinstance(ran.result, WorkflowFailureError)
            if case != "no opt-in":
                cause = ran.result.cause
                assert isinstance(cause, ApplicationError)
                assert cause.type == "StreamReplayError"
                failed = _events(ran.history, EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED)
                assert not failed


@workflow.defn
class _ReadAllWorkflow:
    @workflow.run
    async def run(self, stream_id: str, limits: dict[str, int]) -> list[int]:
        stream = workflow.get_stream(StreamRef[int](id=stream_id))
        records = stream.read(
            wait=timedelta(minutes=5),
            max_records=limits.get("max_records"),
            max_bytes=limits.get("max_bytes"),
        )
        bodies: list[int] = []
        async for record in records:
            bodies.append(record.body)
            # A Stream has no end, so the reader stops at the writer's last Record.
            if record.body == 3:
                break
        return bodies


async def test_a_workflow_read_asks_the_store_with_its_limits(client: Client) -> None:
    store = FaultStore()
    client = stream_client(client, store)
    stream = await client.create_stream(id="paged", body_type=int)
    await stream.append(1, 2, 3)

    # A read with no limit asks for 256 Records and 4 MiB. SDK Core asks the Store for
    # room for one Record more, since it acts on whether the Stream has more. That
    # Record has at most the Record size of the config and an offset of 1024 bytes,
    # each in its field. A Store call waits no longer than the read time of the
    # Worker, which is shorter than the wait of the read.
    def field(size: int) -> int:
        return 1 + (size.bit_length() + 6) // 7 + size

    one_more = field(StreamConfig([store]).max_record_bytes + field(1024))
    cases: list[tuple[dict[str, int], tuple[int, int]]] = [
        ({"max_records": 1, "max_bytes": 100}, (2, 100 + one_more)),
        ({}, (257, 4 * 1024 * 1024 + one_more)),
    ]
    read_time = timedelta(seconds=2)
    for limits, expected in cases:
        with failure_note(f"limits {limits}"):
            store.calls.clear()
            options = RunOptions(
                _ReadAllWorkflow,
                ["paged", limits],
                worker={"stream_read_time": read_time},
            )
            ran = await run(client, options)
            assert ran.result == [1, 2, 3]
            reads = [r for r in store.requests if isinstance(r, ReadRecordsRequest)]
            asked = [(r.max_records, r.max_bytes) for r in reads]
            # With one Record in a page, the read of each Record is one Store call.
            assert asked == [expected] * (3 if limits else 1)
            waits = [r.wait.ToTimedelta() for r in reads]
            assert timedelta(0) < waits[0] and max(waits) <= read_time
            # A replay asks for the Records that History recorded, and no more.
            store.calls.clear()
            await Replayer(
                workflows=[_ReadAllWorkflow],
                stream_config=client._config["stream_config"],
            ).replay_workflow(ran.history)
            replayed = [r for r in store.requests if isinstance(r, ReadRecordsRequest)]
            assert [r.max_records for r in replayed] == ([1] * 3 if limits else [3])


@workflow.defn
class _QuietReadWorkflow:
    @workflow.signal
    def wake(self) -> None:
        pass

    @workflow.run
    async def run(self, stream_id: str) -> list[int]:
        stream = workflow.get_stream(StreamRef[int](id=stream_id))
        bodies: list[int] = []
        offset = None
        # The writer's last Record is 2, since a Stream has no end.
        while 2 not in bodies:
            page = await stream.read_page(from_offset=offset, wait=timedelta(minutes=5))
            bodies.extend(record.body for record in page.records)
            offset = page.last_offset
        return bodies


async def test_a_workflow_read_of_a_quiet_stream_makes_no_workflow_task(
    client: Client,
) -> None:
    store = FaultStore()
    client = stream_client(client, store)
    stream = await client.create_stream(id="quiet", body_type=int)
    completed = EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED

    async def write(handle: Any, _worker: Any) -> None:
        async def first_task() -> None:
            assert _events(await handle.fetch_history(), completed)

        # On a quiet Stream, the reads get pages with no Record until the read limit,
        # and the task then completes with no new task, over several read limits.
        await assert_eventually(first_task)
        await asyncio.sleep(1)
        history = await handle.fetch_history()
        assert len(_events(history, EventType.EVENT_TYPE_WORKFLOW_TASK_STARTED)) == 1
        quiet = [r for r in store.requests if isinstance(r, ReadRecordsRequest)]
        assert quiet and not any(r.from_offset for r in quiet)
        # The read starts again from the same offset in the task of a Signal.
        await stream.append(1, 2)
        await handle.signal(_QuietReadWorkflow.wake)

    options = RunOptions(
        _QuietReadWorkflow,
        ["quiet"],
        worker={"stream_read_time": timedelta(milliseconds=300)},
        during=write,
        replay=True,
    )
    assert (await run(client, options)).result == [1, 2]


@workflow.defn
class _FirstRecordWorkflow:
    @workflow.signal
    def wake(self) -> None:
        pass

    @workflow.run
    async def run(self, stream_id: str) -> list[int]:
        stream = workflow.get_stream(StreamRef[int](id=stream_id))
        async for record in stream.read(wait=timedelta(seconds=5)):
            return [record.body]
        return []


async def test_a_workflow_read_goes_on_after_the_read_limit_cut_its_wait(
    client: Client,
) -> None:
    store = FaultStore()
    client = stream_client(client, store)
    stream = await client.create_stream(id="cut", body_type=int)

    async def write(handle: Any, _worker: Any) -> None:
        async def cut_page() -> None:
            history = await handle.fetch_history()
            markers = _stream_markers(history, "read")
            assert any(b'"wait_cut":true' in m.SerializeToString() for m in markers)
            assert _events(history, EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED)

        # The read limit cuts the wait of 5 s at 1 s. The iterator reads again, and
        # that read waits for the next task, which a Signal opens.
        await assert_eventually(cut_page)
        await stream.append(1)
        await handle.signal(_FirstRecordWorkflow.wake)

    options = RunOptions(
        _FirstRecordWorkflow,
        ["cut"],
        worker={"stream_read_time": timedelta(seconds=1)},
        during=write,
        replay=True,
    )
    assert (await run(client, options)).result == [1]


async def test_a_workflow_read_with_no_wait_reads_again_in_the_next_task(
    client: Client,
) -> None:
    store, hold = FaultStore(), Hold()
    stream = await stream_client(client, store).create_stream(id="slow", body_type=int)
    await stream.append(1, 2, 3)
    store.faults["read_records"].append(hold)

    async def release(handle: Any, _worker: Any) -> None:
        async def task_completed() -> None:
            history = await handle.fetch_history()
            completed = EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
            assert _events(history, completed)

        # The read goes on at the read limit, since only the Store can say that the
        # Stream has no Record. The task then completes, which stops the held read,
        # and the read starts again in the task of the Signal.
        await assert_eventually(task_completed)
        await asyncio.wait_for(hold.cancelled.wait(), 10)
        await handle.signal(_ReadOnceWorkflow.go_on)

    options = RunOptions(
        _ReadOnceWorkflow,
        ["slow"],
        task_timeout=timedelta(seconds=2),
        during=release,
        replay=True,
    )
    assert (await run(stream_client(client, store), options)).result == [1, 2, 3]
    reads = [r for r in store.requests if isinstance(r, ReadRecordsRequest)]
    assert len([r for r in reads if not r.from_offset]) >= 2


@dataclasses.dataclass
class _Turn:
    text: str


@dataclasses.dataclass
class _AgentInput:
    turns: StreamRef[_Turn] | None = None


@dataclasses.dataclass
class _TurnsInput:
    turns: StreamRef[_Turn]
    texts: list[str]


@activity.defn
async def _append_turns(input: _TurnsInput) -> None:
    stream = activity.client().get_stream(input.turns)
    await stream.append(*(_Turn(text) for text in input.texts))


@workflow.defn
class _AgentWorkflow:
    def __init__(self) -> None:
        self._turns: StreamRef[_Turn] | None = None
        self._go_on = False

    @workflow.query
    def turns(self) -> StreamRef[_Turn] | None:
        return self._turns

    @workflow.signal
    def go_on(self) -> None:
        self._go_on = True

    @workflow.run
    async def run(self, input: _AgentInput) -> list[str]:
        self._turns = input.turns
        await workflow.wait_condition(lambda: self._go_on)
        if self._turns is None:
            created = await workflow.create_stream(id="turns", body_type=_Turn)
            self._turns = created.ref
        await workflow.execute_activity(
            _append_turns,
            _TurnsInput(self._turns, ["a", "b"]),
            start_to_close_timeout=timedelta(seconds=10),
        )
        page = await workflow.get_stream(self._turns).read_page()
        # Only a body that the read converted to its type has the attribute.
        return [record.body.text for record in page.records]


async def test_a_stream_ref_keeps_its_type_through_a_workflow(client: Client) -> None:
    streams = stream_client(client, MemoryStreamStore())
    ref: Any = None
    # The first run gets no ref and creates the Stream. The second one gets that ref.
    for texts in (["a", "b"], ["a", "b", "a", "b"]):
        with failure_note(f"texts {texts}"):

            async def answer(handle: Any, _worker: Any) -> None:
                nonlocal ref
                assert await handle.query(_AgentWorkflow.turns) == ref
                await handle.signal(_AgentWorkflow.go_on)
                await handle.result()
                ref = await handle.query(_AgentWorkflow.turns)

            options = RunOptions(
                _AgentWorkflow,
                [_AgentInput(ref)],
                activities=[_append_turns],
                during=answer,
                replay=True,
            )
            assert (await run(streams, options)).result == texts
            assert ref == StreamRef(id="turns", store="memory")
            # The ref of the query has the body type, so the read needs no `body_type=`.
            page = await streams.get_stream(ref).read_page()
            assert [record.body for record in page.records] == [
                _Turn(text) for text in texts
            ]
