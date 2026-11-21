"""Tests of Streams with a Worker on the test server."""

import dataclasses
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
    CloseStreamResponse,
    CreateStreamRequest,
    ReadRecordsRequest,
)
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client, WorkflowFailureError
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
)
from temporalio.exceptions import ApplicationError, StreamError
from temporalio.nexus.system.stream_service import (
    StreamIdConflictPolicy,
    StreamRef,
    StreamStatus,
)
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
            elif operation == "close":
                await stream.close()
                answers.append("closed")
            else:
                described = await stream.describe()
                answers.append([described.status, (described.last_offset or b"").hex()])
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


def _without_writer(request: Message) -> Message:
    """Return a request without what differs between two writers of the same call."""
    copy = type(request)()
    copy.CopyFrom(request)
    for name in ("producer", "request_id"):
        if name in copy.DESCRIPTOR.fields_by_name:
            copy.ClearField(name)
    return copy


async def test_workflow_code_and_a_client_get_the_same_answers(client: Client) -> None:
    for seed in range(8):
        with failure_note(f"seed {seed}"):
            rng = random.Random(seed)
            calls: list[list[Any]] = []
            created: set[str] = set()
            for _ in range(rng.randrange(4, 12)):
                stream_id = f"s-{seed}-{rng.randrange(3)}"
                match rng.choice(["create", "append", "append", "close", "describe"]):
                    # Each create of one run has the same request id, so a second create
                    # of a Stream is the first one again for a Workflow and not for a client.
                    case "create" if stream_id not in created:
                        created.add(stream_id)
                        calls.append(["create", stream_id, rng.randrange(3)])
                    case "create":
                        calls.append(["describe", stream_id])
                    case "append":
                        bodies = [
                            rng.randrange(100) for _ in range(rng.randrange(1, 4))
                        ]
                        calls.append(["append", stream_id, bodies])
                    case operation:
                        calls.append([operation, stream_id])
            # The two Stores have one name and the same Stream ids, so a call is the same
            # request in both.
            for_client, for_workflow = FaultStore(), FaultStore()
            from_client = await _answers(stream_client(client, for_client), calls)
            workflow_client = stream_client(client, for_workflow)
            ran = await run(workflow_client, RunOptions(_CallsWorkflow, [calls]))
            assert ran.result == from_client
            assert [_without_writer(request) for request in for_workflow.requests] == [
                _without_writer(request) for request in for_client.requests
            ]
            appends = [
                request.request_id
                for request in for_workflow.requests
                if isinstance(request, AppendRecordsRequest)
            ]
            assert all(appends) and len(set(appends)) == len(appends)
            # A replay makes no call, with the Stores of the run and with none.
            requests = list(for_workflow.requests)
            for replay_client in (workflow_client, client):
                await Replayer(
                    workflows=[_CallsWorkflow],
                    stream_config=replay_client._config["stream_config"],
                ).replay_workflow(ran.history)
            assert for_workflow.requests == requests


def _bodies(writer: str) -> list[Any]:
    """Return the bodies that a writer appends. One is a value that a pass of external
    storage would take for a reference into the storage of the Worker.
    """
    claim = ExternalStorageReference(
        driver_name="test-driver", claim_data={"key": "payload-0"}
    )
    return [f"{writer} plain", {"writer": writer}, claim]


@activity.defn
async def _append_bodies(stream_id: str) -> None:
    stream = activity.client().get_stream(StreamRef(id=stream_id))
    await stream.append(*_bodies("activity"))


@workflow.defn
class _BodiesWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        await workflow.execute_activity(
            _append_bodies, stream_id, start_to_close_timeout=timedelta(seconds=10)
        )
        stream = workflow.get_stream(StreamRef(id=stream_id))
        await stream.append(*_bodies("workflow"))


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
    await run(client, options)

    expected = [*_bodies("client"), *_bodies("activity"), *_bodies("workflow")]
    assert [record.body async for record in stream.read()] == expected
    # The Store has each body as the codec gave it with no context, and the storage of
    # the Worker has none of them.
    stored = await store.memory.read_records(ReadRecordsRequest(stream_id="bodies"))
    assert [dict(record.body.metadata) for record in stored.records] == [
        {"encoding": b"test/wrapped", "context": b"NoneType"}
    ] * len(expected)
    assert driver._storage and not any(
        b" plain" in value for value in driver._storage.values()
    )


def _events(history: Any, event_type: EventType.ValueType) -> list[Any]:
    return [event for event in history.events if event.event_type == event_type]


async def test_a_store_error_is_the_same_error_on_every_path(client: Client) -> None:
    store = FaultStore()
    client = stream_client(client, store)
    stream = await client.create_stream(id="errors", body_type=int)
    append: list[Any] = ["append", "errors", [1]]
    appends = store.faults["append_records"]
    unavailable = StreamErrorReason.STREAM_ERROR_REASON_STORE_UNAVAILABLE

    async def answers_on(path: str, calls: list[list[Any]], **options: Any) -> Any:
        if path == "client":
            return await _answers(client, calls)
        args = [calls, path == "activity"]
        run_options = RunOptions(
            _CallsWorkflow, args, activities=[_calls_activity], **options
        )
        return (await run(client, run_options)).result

    def append_requests() -> list[AppendRecordsRequest]:
        requests = [r for r in store.requests if isinstance(r, AppendRecordsRequest)]
        del store.requests[:]
        return requests

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
            requests = append_requests()
            assert len(requests) == len(faults) + 1
            assert len({request.request_id for request in requests}) == 1
            assert not _events(ran.history, EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED)
    # A client and an Activity get the outage as the error of their call.
    for path in ("client", "activity"):
        appends.append(raises(ConnectionError("The connection is gone")))
        [[error, reason, _]] = await answers_on(path, [append])
        assert (error, reason, len(append_requests())) == ("error", unavailable, 1)

    # An answer of the wrong message fails the call, and nothing tries it again.
    corrupt = StreamErrorReason.STREAM_ERROR_REASON_STORE_CORRUPT
    for path in ("client", "activity", "workflow"):
        with failure_note(path):
            appends.append(answers(CloseStreamResponse()))
            [[error, reason, _]] = await answers_on(path, [append])
            assert (error, reason, len(append_requests())) == ("error", corrupt, 1)

    # A refusal is the same error for a client, an Activity and Workflow code. Workflow
    # code gets it again in each task that replays the call.
    await stream.close()
    calls = [append, ["describe", "errors"], ["describe", "missing"]]
    refused = await answers_on("client", calls)
    assert [answer[:2] for answer in refused] == [
        ["error", StreamErrorReason.STREAM_ERROR_REASON_CLOSED],
        [StreamStatus.StreamStatusClosed, (3).to_bytes(8, "big").hex()],
        ["error", StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND],
    ]
    for path in ("activity", "workflow"):
        with failure_note(path):
            assert await answers_on(path, calls, cached=False, replay=True) == refused

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
        ["close", "s"],
        ["describe", "s"],
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


@workflow.defn
class _ClosingWorkflow:
    @workflow.run
    async def run(self, stream_id: str, how: str) -> None:
        stream = await workflow.create_stream(id=stream_id, body_type=str)
        if how == "async with":
            async with stream:
                await stream.append("body")
            return
        try:
            await stream.append("body")
            if how == "failure":
                raise ApplicationError("The block failed")
            if how == "cancel":
                await workflow.wait_condition(lambda: False)
        finally:
            await stream.close()


async def test_a_close_of_workflow_code_closes_the_stream(client: Client) -> None:
    for how in ("async with", "return", "failure", "cancel"):
        with failure_note(how):
            store = FaultStore()
            stream_id = f"closed after a {how}"

            async def cancel_in_the_block(handle: Any, _worker: Any) -> None:
                async def appended() -> None:
                    assert any(
                        isinstance(r, AppendRecordsRequest) for r in store.requests
                    )

                await assert_eventually(appended)
                await handle.cancel()

            options = RunOptions(_ClosingWorkflow, [stream_id, how])
            if how == "cancel":
                options.during = cancel_in_the_block
            ran = await run(stream_client(client, store), options)
            assert (ran.result is None) == (how in ("async with", "return"))
            page = await store.memory.read_records(
                ReadRecordsRequest(stream_id=stream_id)
            )
            assert (len(page.records), page.done) == (1, True)


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
    assert ran.result == [["memory", True], [StreamStatus.StreamStatusOpen, ""]]
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
            targets: list[Any] = []

            def pick_first(context: Any) -> FaultStore:
                targets.append(context.target)
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
            assert [(type(r).__name__, r.stream_id) for r in first.requests] == [
                ("CreateStreamRequest", "own"),
            ]
            # After the swap, Core calls the Store objects of the new client.
            assert [(type(r).__name__, r.stream_id) for r in new_first.requests] == [
                ("AppendRecordsRequest", "own"),
            ]
            assert [(type(r).__name__, r.stream_id) for r in new_second.requests] == [
                ("CreateStreamRequest", "shared"),
                ("AppendRecordsRequest", "shared"),
                ("AppendRecordsRequest", "shared"),
            ]
            assert not second.requests
            # The selector of the Workflow's create got the Workflow as its target.
            assert [type(t).__name__ for t in targets] == ["StorageDriverWorkflowInfo"]


async def test_an_append_over_a_limit_of_the_config_never_reaches_the_store(
    client: Client,
) -> None:
    too_large = StreamErrorReason.STREAM_ERROR_REASON_TOO_LARGE
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
            options = RunOptions(
                _CallsWorkflow, [calls(f"workflow {seed}")], replay=True
            )
            from_workflow = (await run(streams, options)).result
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
    creates = [r for r in store.requests if isinstance(r, CreateStreamRequest)]
    assert len(creates) == 4 and len({create.request_id for create in creates}) == 1
