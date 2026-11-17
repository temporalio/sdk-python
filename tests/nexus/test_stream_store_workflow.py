from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

import pytest
from google.protobuf.message import Message

import temporalio.common
import temporalio.exceptions
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType, StreamErrorReason
from temporalio.api.streamservice.v1 import (
    CreateStreamRequest as CreateStreamRequestMessage,
)
from temporalio.api.streamservice.v1 import (
    CreateStreamResponse as CreateStreamResponseMessage,
)
from temporalio.api.streamservice.v1 import (
    DescribeStreamRequest as DescribeStreamRequestMessage,
)
from temporalio.api.streamservice.v1 import (
    DescribeStreamResponse as DescribeStreamResponseMessage,
)
from temporalio.client import (
    Client,
    StreamConfig,
    StreamSelectContext,
    StreamStore,
    StreamStoreCallOptions,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.common import RetryPolicy
from temporalio.converter import DataConverter, StorageDriverWorkflowInfo
from temporalio.exceptions import ApplicationError, StreamError
from temporalio.nexus.system import TEMPORAL_SYSTEM_ENDPOINT
from temporalio.nexus.system.stream_service import (
    AppendRecordsResponse,
    ClientProducer,
    Producer,
    ProducerKindClient,
    ProducerKindLocalActivity,
    ProducerKindWorkflow,
    ReadRecordsRequest,
    StreamIdConflictPolicy,
    StreamRecord,
    StreamRef,
    StreamService,
    StreamStatus,
)
from temporalio.nexus.system.stream_service.workflow import create_stream, get_stream
from temporalio.nexus.system.stream_service.workflow._resources.stream import (
    Stream as WorkflowStream,
)
from temporalio.testing import MemoryStreamStore
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
    Replayer,
    Worker,
)
from tests.helpers import assert_task_fail_eventually, new_worker
from tests.helpers.streams import DictStreamStore, RecordingStreamStore
from tests.test_converter import SimpleCodec

# Workflow code writes these values, so the History shows only whether a Record body
# leaks into it.
_BODIES = ["first-record-body", "second-record-body"]


@activity.defn
async def append_values(ref: StreamRef[str], values: list[str]) -> None:
    await activity.client().get_stream(ref, body_type=str).append(*values)


async def _append_in_local_activity(ref: StreamRef[str], values: list[str]) -> None:
    await workflow.execute_local_activity(
        append_values, args=[ref, values], start_to_close_timeout=timedelta(seconds=10)
    )


@workflow.defn
class AppendWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> StreamRef[str]:
        stream = await create_stream(id=stream_id, body_type=str)
        await stream.append(*_BODIES)
        return stream.ref


@workflow.defn
class LocalActivityAppendWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> StreamRef[str]:
        stream = await create_stream(id=stream_id, body_type=str)
        await _append_in_local_activity(stream.ref, _BODIES)
        return stream.ref


# Workflow code appends either with the Workflow `Stream` or with a Local Activity of
# its own. Core makes the first append in the Workflow Task as the Workflow, and the
# second writes as the Local Activity that runs it.
_APPEND_WORKFLOWS = pytest.mark.parametrize(
    "append_workflow", [AppendWorkflow, LocalActivityAppendWorkflow]
)


@workflow.defn
class AppendOffsetsWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> AppendRecordsResponse:
        stream = await create_stream(id=stream_id, body_type=str)
        return await stream.append(*_BODIES)


@workflow.defn
class AppendToStreamWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = get_stream(StreamRef(id=stream_id), body_type=str)
        try:
            await stream.append("a")
        except StreamError as err:
            assert isinstance(err, ApplicationError)
            return StreamErrorReason.Name(err.reason)
        return "appended"


@workflow.defn
class ExplicitProducerWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = await create_stream(id=stream_id, body_type=str)
        try:
            await stream.append(
                "a",
                producer=Producer(id="mine", kind=ProducerKindClient(ClientProducer())),
            )
        except StreamError as err:
            return f"{StreamErrorReason.Name(err.reason)}: {err.message}"
        return "appended"


@workflow.defn
class NexusClientReadWorkflow:
    @workflow.run
    async def run(self, operation: str) -> str:
        # Workflow code can also start the operation of `read` through a Nexus client.
        requests: dict[str, Any] = {
            "ReadRecords": ReadRecordsRequest(ref=StreamRef[str](id="stream")),
        }
        nexus_client = workflow.create_nexus_client(
            service=StreamService, endpoint=TEMPORAL_SYSTEM_ENDPOINT
        )
        try:
            handle: workflow.NexusOperationHandle[
                Any
            ] = await nexus_client.start_operation(operation, requests[operation])
            await handle
        except StreamError as err:
            return StreamErrorReason.Name(err.reason)
        except ApplicationError as err:
            assert err.non_retryable
            return err.message
        return "read"


@workflow.defn
class DescribeResultWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = await create_stream(id=stream_id, body_type=str)
        try:
            await stream.describe()
        except StreamError as err:
            return f"StreamError {StreamErrorReason.Name(err.reason)}: {err.message}"
        except ApplicationError as err:
            return f"ApplicationError {err.type}: {err.message}"
        except temporalio.exceptions.TimeoutError as err:
            return f"TimeoutError {err.type and err.type.name}"
        return "described"


@workflow.defn
class DescribeStreamWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = get_stream(StreamRef(id=stream_id), body_type=str)
        try:
            await stream.describe()
        except StreamError as err:
            assert isinstance(err, ApplicationError)
            return StreamErrorReason.Name(err.reason)
        return "described"


@workflow.defn
class CreateThenContinueAsNewWorkflow:
    @workflow.run
    async def run(self, stream_id: str, run: int) -> list[bool]:
        stream = await create_stream(id=stream_id, body_type=str)
        await _append_in_local_activity(stream.ref, [f"run-{run}"])
        if run == 1:
            workflow.continue_as_new(args=[stream_id, 2])
        return [bool(stream.created)]


@workflow.defn
class DescribeOtherStoreWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        await get_stream(
            StreamRef(id=stream_id, store="other"), body_type=str
        ).describe()


@workflow.defn
class CreateAndDescribeWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> StreamRef[str]:
        stream = await create_stream(id=stream_id, body_type=str)
        await stream.describe()
        return stream.ref


class CountingStore(RecordingStreamStore):
    def __init__(self, name: str) -> None:
        super().__init__(MemoryStreamStore(name=name))


@activity.defn
async def user_activity() -> None:
    pass


@workflow.defn
class CreateAndActivityWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        await create_stream(id=stream_id, body_type=str)
        await workflow.execute_local_activity(
            user_activity, start_to_close_timeout=timedelta(seconds=10)
        )


_fail_next_task = True


@workflow.defn(sandboxed=False)
class AppendThenFailTaskOnceWorkflow:
    @workflow.run
    async def run(self, stream_id: str, in_local_activity: bool) -> None:
        stream = await create_stream(
            id=stream_id,
            body_type=str,
            id_conflict_policy=StreamIdConflictPolicy.StreamIdConflictPolicyUseExisting,
        )
        if in_local_activity:
            await _append_in_local_activity(stream.ref, _BODIES)
        else:
            await stream.append(*_BODIES)
        global _fail_next_task
        if _fail_next_task:
            _fail_next_task = False
            raise RuntimeError("Failed the Workflow Task once")


@activity.defn
async def append_from_local_activity(stream_id: str) -> None:
    stream = activity.client().get_stream(StreamRef(id=stream_id), body_type=str)
    await stream.append("a")


@workflow.defn
class UserLocalActivityAppendWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        await workflow.execute_local_activity(
            append_from_local_activity,
            stream_id,
            start_to_close_timeout=timedelta(seconds=10),
        )


class FlakyDescribeStore(RecordingStreamStore):
    def __init__(self, *, failures: int = 0, delay: timedelta = timedelta()) -> None:
        super().__init__(MemoryStreamStore())
        self._failures = failures
        self._delay = delay
        self.describes = 0

    async def before_call(self, operation: str, request: Message) -> None:
        if operation == "DescribeStream":
            self.describes += 1
            await asyncio.sleep(self._delay.total_seconds())
            if self.describes <= self._failures:
                raise RuntimeError("describe failed")


class FlakyCreateStore(CountingStore):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.failed_creates = 0

    async def before_call(self, operation: str, request: Message) -> None:
        if operation == "CreateStream" and not self.failed_creates:
            self.failed_creates += 1
            raise RuntimeError("create failed")


@workflow.defn
class CloseWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> StreamRef[str]:
        stream = await create_stream(id=stream_id, body_type=str)
        await stream.append("a")
        await stream.close()
        return stream.ref


@workflow.defn
class AsyncWithCloseWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        async with create_stream(id=stream_id, body_type=str) as stream:
            await stream.append("a")
            described = await stream.describe()
        assert described.status is not None
        return described.status.name


_fail_close_intent_task = True


@workflow.defn(sandboxed=False)
class CloseIntentThenFailTaskOnceWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        stream = get_stream(StreamRef(id=stream_id), body_type=str)
        await stream.append("a")
        close = asyncio.create_task(stream.close())
        # A few turns of the loop let the close send its intent in this task.
        for _ in range(10):
            await asyncio.sleep(0)
        global _fail_close_intent_task
        if _fail_close_intent_task:
            _fail_close_intent_task = False
            raise RuntimeError("Failed the Workflow Task once")
        await close


class RecordingInterceptor(Interceptor):
    def __init__(self) -> None:
        self.activity_types: list[str] = []
        self.is_local: list[bool] = []

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _RecordingActivityInbound(next, self)


class _RecordingActivityInbound(ActivityInboundInterceptor):
    def __init__(
        self, next: ActivityInboundInterceptor, recording: RecordingInterceptor
    ) -> None:
        super().__init__(next)
        self._recording = recording

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        self._recording.activity_types.append(info.activity_type)
        self._recording.is_local.append(info.is_local)
        return await super().execute_activity(input)


def _with_store(client: Client, store: StreamStore) -> Client:
    return _with_config(client, StreamConfig(stores=[store]))


def _with_config(client: Client, stream_config: StreamConfig) -> Client:
    config = client.config()
    config["stream_config"] = stream_config
    return Client(**config)


def _two_counting_stores(
    contexts: list[StreamSelectContext],
) -> tuple[CountingStore, CountingStore, StreamConfig]:
    first = CountingStore("first")
    second = CountingStore("second")

    def select(context: StreamSelectContext) -> StreamStore:
        contexts.append(context)
        return second

    return first, second, StreamConfig(stores=[first, second], store_selector=select)


async def _run_create_and_describe(
    client: Client, stream_id: str
) -> tuple[WorkflowHandle[CreateAndDescribeWorkflow, StreamRef[str]], StreamRef[str]]:
    async with new_worker(client, CreateAndDescribeWorkflow) as worker:
        handle = await client.start_workflow(
            CreateAndDescribeWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        return handle, await handle.result()


async def _read_all(client: Client, stream_id: str) -> list[StreamRecord[str]]:
    stream = client.get_stream(StreamRef(id=stream_id), body_type=str)
    return [record async for record in stream.read()]


def _epoch(producer: Producer) -> tuple[int, int, int]:
    # The Workflow Task, then the attempt of the Local Activity, which a Workflow lacks.
    if isinstance(producer.kind, ProducerKindWorkflow):
        task = producer.kind.value.epoch
        assert task is not None
        return (task.started_event_id, task.attempt, 0)
    assert isinstance(producer.kind, ProducerKindLocalActivity)
    local = producer.kind.value.epoch
    return (local.task.started_event_id, local.task.attempt, local.attempt)


def _producers(records: list[StreamRecord[str]]) -> list[Producer]:
    producers = [record.producer for record in records]
    assert all(producer is not None for producer in producers)
    return [producer for producer in producers if producer is not None]


async def _run_append_workflow(
    client: Client, append_workflow: type[Any] = AppendWorkflow
) -> tuple[WorkflowHandle[Any, StreamRef[str]], StreamRef[str]]:
    async with new_worker(
        client, append_workflow, activities=[append_values]
    ) as worker:
        handle: WorkflowHandle[Any, StreamRef[str]] = await client.start_workflow(
            append_workflow.run,
            f"stream-{uuid.uuid4()}",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        return handle, await handle.result()


async def _describe_result(
    client: Client,
    store: StreamStore,
    options: StreamStoreCallOptions | None = None,
) -> str:
    stream_config = StreamConfig(
        stores=[store], workflow_call_options=options or StreamStoreCallOptions()
    )
    stream_client = _with_config(client, stream_config)
    async with new_worker(stream_client, DescribeResultWorkflow) as worker:
        return await stream_client.execute_workflow(
            DescribeResultWorkflow.run,
            f"stream-{uuid.uuid4()}",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )


@_APPEND_WORKFLOWS
async def test_workflow_stream_uses_store(client: Client, append_workflow: type[Any]):
    stream_client = _with_store(client, MemoryStreamStore())
    handle, ref = await _run_append_workflow(stream_client, append_workflow)

    stream = stream_client.get_stream(ref, body_type=str)
    records = [record async for record in stream.iter_read_items()]
    assert [record.body for record in records] == _BODIES
    run_id = handle.result_run_id
    assert run_id
    first, second = _producers(records)
    assert first == second
    if append_workflow is AppendWorkflow:
        # The Workflow writes as itself, with its first Workflow Task as the epoch.
        assert first.id == f"wf:{run_id}"
        assert isinstance(first.kind, ProducerKindWorkflow)
        assert first.kind.value.run_id == run_id
        assert first.kind.value.epoch is not None
        assert first.kind.value.epoch.started_event_id > 0
        assert first.kind.value.epoch.attempt == 1
        return
    # The create is the run's first Local Activity, so the append is its second.
    assert first.id == f"la:{run_id}:2"
    assert isinstance(first.kind, ProducerKindLocalActivity)
    assert first.kind.value.run_id == run_id
    assert first.kind.value.epoch.task.started_event_id > 0
    assert first.kind.value.epoch.attempt == 1


async def test_workflow_stream_uses_a_store_written_from_scratch(client: Client):
    store = DictStreamStore()
    stream_client = _with_store(client, store)
    stream_id = f"stream-{uuid.uuid4()}"
    _, ref = await _run_create_and_describe(stream_client, stream_id)
    assert ref == StreamRef(id=stream_id, store="dict")
    assert list(store.streams) == [stream_id]

    stream = stream_client.get_stream(ref, body_type=str)
    await stream.append("a")
    assert [record.body async for record in stream.iter_read_items()] == ["a"]


@_APPEND_WORKFLOWS
async def test_workflow_stream_store_uses_payload_codec(
    client: Client, append_workflow: type[Any]
):
    store = MemoryStreamStore()
    config = client.config()
    config["data_converter"] = DataConverter(payload_codec=SimpleCodec())
    config["stream_config"] = StreamConfig(stores=[store])
    stream_client = Client(**config)
    _, ref = await _run_append_workflow(stream_client, append_workflow)

    # A client without the codec reads the bodies as the Store keeps them.
    raw = _with_store(client, store).get_stream(
        StreamRef(id=ref.id), body_type=temporalio.common.RawValue
    )
    stored = [record.body.payload async for record in raw.iter_read_items()]
    assert [body.metadata["simple-codec"] for body in stored] == [b"true", b"true"]
    stream = stream_client.get_stream(ref, body_type=str)
    assert [record.body async for record in stream.iter_read_items()] == _BODIES


@_APPEND_WORKFLOWS
async def test_workflow_stream_history_has_no_bodies(
    client: Client, append_workflow: type[Any]
):
    stream_client = _with_store(client, MemoryStreamStore())
    handle, _ = await _run_append_workflow(stream_client, append_workflow)
    events = (await handle.fetch_history()).events
    markers = [
        event.marker_recorded_event_attributes.marker_name
        for event in events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    ]
    # One marker for the create and one for the append. Core writes its own marker for
    # an append that it makes in the Workflow Task.
    append_marker = (
        "core_stream" if append_workflow is AppendWorkflow else "core_local_activity"
    )
    assert markers == ["core_local_activity", append_marker]
    for event in events:
        for body in _BODIES:
            assert body.encode() not in event.SerializeToString()


@_APPEND_WORKFLOWS
async def test_workflow_stream_replay_does_not_call_store(
    client: Client, append_workflow: type[Any]
):
    stream_client = _with_store(client, MemoryStreamStore())
    handle, ref = await _run_append_workflow(stream_client, append_workflow)
    # A replay of an append that Core made needs a Store that runs on the bridge, as the
    # first run had. A new memory Store shows any call, since it has no Stream.
    replay_store = MemoryStreamStore()
    await Replayer(
        workflows=[append_workflow],
        stream_config=StreamConfig(stores=[replay_store]),
    ).replay_workflow(await handle.fetch_history())
    with pytest.raises(StreamError) as err:
        await (
            _with_store(client, replay_store).get_stream(ref, body_type=str).describe()
        )
    assert err.value.reason == StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND


async def test_workflow_create_without_stream_config_fails_the_workflow(
    client: Client,
):
    async with new_worker(client, CreateAndDescribeWorkflow) as worker:
        handle = await client.start_workflow(
            CreateAndDescribeWorkflow.run,
            f"stream-{uuid.uuid4()}",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        with pytest.raises(WorkflowFailureError) as err:
            await asyncio.wait_for(handle.result(), timeout=10)
    cause = err.value.cause
    assert isinstance(cause, ApplicationError)
    assert not isinstance(cause, StreamError)
    assert cause.type == "StreamConfigError"
    assert cause.non_retryable
    assert cause.message == (
        "The client of this Worker has no Stream Store. Give one in "
        "stream_config=StreamConfig(stores=[...])."
    )
    # A Replayer with no Stream config fails the Workflow the same way.
    await Replayer(workflows=[CreateAndDescribeWorkflow]).replay_workflow(
        await handle.fetch_history()
    )


async def test_workflow_read_through_a_nexus_client_goes_through_core(
    client: Client,
):
    async def read_with(store: StreamStore) -> str:
        stream_client = _with_store(client, store)
        async with new_worker(stream_client, NexusClientReadWorkflow) as worker:
            return await stream_client.execute_workflow(
                NexusClientReadWorkflow.run,
                "ReadRecords",
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )

    # Core reads the Store, as for `read`, also one written in Python. Nobody created
    # the Stream.
    assert await read_with(MemoryStreamStore()) == "STREAM_ERROR_REASON_NOT_FOUND"
    assert await read_with(DictStreamStore()) == "STREAM_ERROR_REASON_NOT_FOUND"


def test_workflow_stream_has_the_calls_of_a_client_stream():
    for name in (
        "append",
        "close",
        "describe",
        "read",
        "iter_read",
        "iter_read_items",
        "__aenter__",
        "__aexit__",
    ):
        assert hasattr(WorkflowStream, name)
    assert not hasattr(WorkflowStream, "iter")


async def test_workflow_stream_store_error_reaches_workflow_code(client: Client):
    store = FlakyDescribeStore(failures=1)
    result = await _describe_result(client, store)
    assert result == "ApplicationError RuntimeError: describe failed"
    assert store.describes == 1


async def test_workflow_describe_of_unknown_stream_raises_stream_error(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    async with new_worker(stream_client, DescribeStreamWorkflow) as worker:
        reason = await stream_client.execute_workflow(
            DescribeStreamWorkflow.run,
            f"stream-{uuid.uuid4()}",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert reason == "STREAM_ERROR_REASON_NOT_FOUND"


async def test_strict_create_in_each_run_of_a_chain_succeeds(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(
        stream_client, CreateThenContinueAsNewWorkflow, activities=[append_values]
    ) as worker:
        handle = await stream_client.start_workflow(
            CreateThenContinueAsNewWorkflow.run,
            args=[stream_id, 1],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == [True]
    first_run_id = handle.first_execution_run_id
    last_run = await stream_client.get_workflow_handle(handle.id).describe()
    assert first_run_id
    assert last_run.run_id != first_run_id
    records = await _read_all(stream_client, stream_id)
    assert [record.body for record in records] == ["run-1", "run-2"]
    first, second = _producers(records)
    assert isinstance(first.kind, ProducerKindLocalActivity)
    assert isinstance(second.kind, ProducerKindLocalActivity)
    assert first.kind.value.run_id == first_run_id
    assert second.kind.value.run_id == last_run.run_id


@_APPEND_WORKFLOWS
async def test_client_append_after_workflow_append_keeps_offset_order(
    client: Client, append_workflow: type[Any]
):
    stream_client = _with_store(client, MemoryStreamStore())
    _, ref = await _run_append_workflow(stream_client, append_workflow)
    stream = stream_client.get_stream(ref, body_type=str)
    appended = await stream.append("from-client")
    records = await _read_all(stream_client, ref.id)
    assert [record.body for record in records] == [*_BODIES, "from-client"]
    offsets = [record.offset for record in records]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)
    assert offsets[-1] == appended.last_offset
    kinds = [type(producer.kind) for producer in _producers(records)]
    workflow_kind = (
        ProducerKindWorkflow
        if append_workflow is AppendWorkflow
        else ProducerKindLocalActivity
    )
    assert kinds == [workflow_kind, workflow_kind, ProducerKindClient]


async def test_workflow_stream_store_call_options_retry(client: Client):
    options = StreamStoreCallOptions(
        retry_policy=RetryPolicy(
            initial_interval=timedelta(milliseconds=10), maximum_attempts=2
        )
    )
    store = FlakyDescribeStore(failures=1)
    assert await _describe_result(client, store, options) == "described"
    assert store.describes == 2


async def test_workflow_stream_store_call_options_timeout(client: Client):
    options = StreamStoreCallOptions(start_to_close_timeout=timedelta(milliseconds=200))
    store = FlakyDescribeStore(delay=timedelta(seconds=5))
    result = await _describe_result(client, store, options)
    assert result == "TimeoutError START_TO_CLOSE"


async def test_worker_keeps_its_stream_stores(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    async with new_worker(stream_client, AppendWorkflow) as worker:
        with pytest.raises(ValueError, match="Stream Stores"):
            worker.client = client
        worker.client = stream_client


async def test_activity_interceptors_see_stream_store_activity(client: Client):
    interceptor = RecordingInterceptor()
    stream_client = _with_store(client, MemoryStreamStore())
    async with new_worker(
        stream_client,
        CreateAndActivityWorkflow,
        activities=[user_activity],
        interceptors=[interceptor],
    ) as worker:
        await stream_client.execute_workflow(
            CreateAndActivityWorkflow.run,
            f"stream-{uuid.uuid4()}",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    # The create runs as the SDK's Local Activity, before the Workflow's own Activity.
    assert interceptor.activity_types == [
        "__temporal_stream_store_call:memory",
        "user_activity",
    ]


@pytest.mark.parametrize("in_local_activity", [False, True])
async def test_workflow_task_retry_appends_with_higher_epoch(
    client: Client, in_local_activity: bool
):
    global _fail_next_task
    _fail_next_task = True
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(
        stream_client, AppendThenFailTaskOnceWorkflow, activities=[append_values]
    ) as worker:
        await stream_client.execute_workflow(
            AppendThenFailTaskOnceWorkflow.run,
            args=[stream_id, in_local_activity],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    records = await _read_all(stream_client, stream_id)
    # The failed Workflow Task records no marker, so its retry appends again and writes
    # a second copy.
    assert [record.body for record in records] == [*_BODIES, *_BODIES]
    producers = _producers(records)
    assert len({producer.id for producer in producers}) == 1
    if not in_local_activity:
        assert producers[0].id.startswith("wf:")

    first, second, third, fourth = (_epoch(producer) for producer in producers)
    assert first == second
    assert third == fourth
    assert third > first


async def test_user_local_activity_append_has_local_activity_producer(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream = await stream_client.create_stream(
        id=f"stream-{uuid.uuid4()}", body_type=str
    )
    async with new_worker(
        stream_client,
        UserLocalActivityAppendWorkflow,
        activities=[append_from_local_activity],
    ) as worker:
        handle = await stream_client.start_workflow(
            UserLocalActivityAppendWorkflow.run,
            stream.ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
    [producer] = _producers(await _read_all(stream_client, stream.ref.id))
    run_id = handle.result_run_id
    assert producer.id == f"la:{run_id}:1"
    assert isinstance(producer.kind, ProducerKindLocalActivity)
    assert producer.kind.value.run_id == run_id
    assert producer.kind.value.epoch.task.started_event_id > 0
    assert producer.kind.value.epoch.attempt == 1


async def test_workflow_ref_of_another_store_fails_the_task(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    async with new_worker(stream_client, DescribeOtherStoreWorkflow) as worker:
        handle = await stream_client.start_workflow(
            DescribeOtherStoreWorkflow.run,
            f"stream-{uuid.uuid4()}",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        await assert_task_fail_eventually(
            handle, message_contains="names the Stream Store 'other'"
        )
        await handle.terminate()


async def test_workflow_create_without_store_on_several_stores_fails_the_task(
    client: Client,
):
    stream_config = StreamConfig(
        stores=[CountingStore("first"), CountingStore("second")]
    )
    stream_client = _with_config(client, stream_config)
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(stream_client, CreateAndDescribeWorkflow) as worker:
        handle = await stream_client.start_workflow(
            CreateAndDescribeWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        await assert_task_fail_eventually(
            handle,
            message_contains=(
                "StreamConfig has more than one Store and no store_selector, and the "
                f"call on the Stream '{stream_id}' names no Store. Name one with "
                "store=, or set StreamConfig.store_selector."
            ),
        )
        await handle.terminate()


async def test_workflow_create_goes_to_the_selected_store(client: Client):
    contexts: list[StreamSelectContext] = []
    first, second, stream_config = _two_counting_stores(contexts)
    stream_id = f"stream-{uuid.uuid4()}"
    handle, ref = await _run_create_and_describe(
        _with_config(client, stream_config), stream_id
    )
    assert ref.store == "second"
    assert first.operations == []
    assert second.operations == ["CreateStream", "DescribeStream"]
    # The describe goes by the Store that the ref names, so the selector runs once.
    assert contexts == [
        StreamSelectContext(
            id=stream_id,
            target=StorageDriverWorkflowInfo(
                id=handle.id,
                type="CreateAndDescribeWorkflow",
                run_id=handle.result_run_id,
                namespace=client.namespace,
            ),
        )
    ]


async def test_workflow_replay_calls_no_store(client: Client):
    _, _, stream_config = _two_counting_stores([])
    stream_id = f"stream-{uuid.uuid4()}"
    handle, _ = await _run_create_and_describe(
        _with_config(client, stream_config), stream_id
    )
    contexts: list[StreamSelectContext] = []
    first, second, replay_config = _two_counting_stores(contexts)
    await Replayer(
        workflows=[CreateAndDescribeWorkflow], stream_config=replay_config
    ).replay_workflow(await handle.fetch_history())
    # The selector runs in Workflow code, so a replay runs it again with the Workflow.
    assert [context.id for context in contexts] == [stream_id]
    target = contexts[0].target
    assert isinstance(target, StorageDriverWorkflowInfo)
    assert (target.id, target.run_id) == (handle.id, handle.result_run_id)
    assert first.operations == []
    assert second.operations == []


async def test_workflow_create_with_several_stores_takes_the_config_options(
    client: Client,
):
    first = CountingStore("first")
    second = FlakyCreateStore("second")
    stream_config = StreamConfig(
        stores=[first, second],
        store_selector=lambda _: second,
        workflow_call_options=StreamStoreCallOptions(
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=10), maximum_attempts=2
            )
        ),
    )
    # The default options make one attempt, so only the config's retry policy lets
    # the create pass after its first attempt fails.
    _, ref = await _run_create_and_describe(
        _with_config(client, stream_config), f"stream-{uuid.uuid4()}"
    )
    assert ref.store == "second"
    assert first.operations == []
    assert second.operations == ["CreateStream", "CreateStream", "DescribeStream"]


class OwnActivityStore(CountingStore):
    """A Store that brings its own Activity for the calls of Workflow code."""

    def __init__(self) -> None:
        super().__init__("own")
        self.workflow_calls: list[str] = []

    def worker_activities(self) -> Sequence[Callable[..., Any]]:
        @activity.defn(name="own_store_call")
        async def own_store_call(operation: str, request: bytes) -> bytes:
            if operation == "CreateStream":
                created = await self.create_stream(
                    CreateStreamRequestMessage.FromString(request)
                )
                return created.SerializeToString()
            described = await self.describe_stream(
                DescribeStreamRequestMessage.FromString(request)
            )
            return described.SerializeToString()

        return [own_store_call]

    async def call_from_workflow(
        self, operation: str, request: Message, options: StreamStoreCallOptions
    ) -> Message:
        self.workflow_calls.append(operation)
        response = await workflow.execute_local_activity(
            "own_store_call",
            args=[operation, request.SerializeToString()],
            result_type=bytes,
            start_to_close_timeout=options.start_to_close_timeout,
        )
        if operation == "CreateStream":
            return CreateStreamResponseMessage.FromString(response)
        return DescribeStreamResponseMessage.FromString(response)


async def test_workflow_call_goes_through_the_store_own_activity(client: Client):
    store = OwnActivityStore()
    interceptor = RecordingInterceptor()
    stream_client = _with_store(client, store)
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(
        stream_client, CreateAndDescribeWorkflow, interceptors=[interceptor]
    ) as worker:
        ref = await stream_client.execute_workflow(
            CreateAndDescribeWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert ref == StreamRef(id=stream_id, store="own")
    assert store.workflow_calls == ["CreateStream", "DescribeStream"]
    assert store.operations == ["CreateStream", "DescribeStream"]
    assert interceptor.activity_types == ["own_store_call", "own_store_call"]


async def test_workflow_append_returns_offsets_of_records_in_order(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(stream_client, AppendOffsetsWorkflow) as worker:
        appended = await stream_client.execute_workflow(
            AppendOffsetsWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    records = await _read_all(stream_client, stream_id)
    assert [record.body for record in records] == _BODIES
    assert [appended.first_offset, appended.last_offset] == [
        record.offset for record in records
    ]


async def test_workflow_append_to_closed_stream_raises_stream_error(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream = await stream_client.create_stream(
        id=f"stream-{uuid.uuid4()}", body_type=str
    )
    await stream.close()
    async with new_worker(stream_client, AppendToStreamWorkflow) as worker:
        reason = await stream_client.execute_workflow(
            AppendToStreamWorkflow.run,
            stream.ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert reason == "STREAM_ERROR_REASON_CLOSED"


async def test_workflow_append_with_a_producer_of_another_kind_fails(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(stream_client, ExplicitProducerWorkflow) as worker:
        result = await stream_client.execute_workflow(
            ExplicitProducerWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    # Core writes only as the Workflow's Producer, so the append writes nothing.
    assert result == (
        "STREAM_ERROR_REASON_INVALID: A Stream append of Workflow code writes as a "
        "Producer of kind Workflow. Leave out producer=, or give a Producer of kind "
        "Workflow."
    )
    assert await _read_all(stream_client, stream_id) == []


async def _describe_status(client: Client, stream_id: str) -> StreamStatus | None:
    stream = client.get_stream(StreamRef(id=stream_id), body_type=str)
    return (await stream.describe()).status


async def test_workflow_async_with_closes_the_stream(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(stream_client, AsyncWithCloseWorkflow) as worker:
        status = await stream_client.execute_workflow(
            AsyncWithCloseWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert status == StreamStatus.StreamStatusOpen.name
    assert await _describe_status(stream_client, stream_id) == (
        StreamStatus.StreamStatusClosed
    )
    records = await _read_all(stream_client, stream_id)
    assert [record.body for record in records] == ["a"]


@pytest.mark.parametrize("no_remote_activities", [False, True])
async def test_worker_with_workflows_only_runs_the_close(
    client: Client, no_remote_activities: bool
):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    async with Worker(
        stream_client,
        task_queue=str(uuid.uuid4()),
        workflows=[CloseWorkflow],
        no_remote_activities=no_remote_activities,
    ) as worker:
        await stream_client.execute_workflow(
            CloseWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert await _describe_status(stream_client, stream_id) == (
        StreamStatus.StreamStatusClosed
    )


async def test_failed_workflow_task_after_direct_close_leaves_the_stream_open(
    client: Client,
):
    global _fail_close_intent_task
    _fail_close_intent_task = True
    stream_client = _with_store(client, MemoryStreamStore())
    stream = await stream_client.create_stream(
        id=f"stream-{uuid.uuid4()}", body_type=str
    )
    async with new_worker(stream_client, CloseIntentThenFailTaskOnceWorkflow) as worker:
        await stream_client.execute_workflow(
            CloseIntentThenFailTaskOnceWorkflow.run,
            stream.ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    # Core makes the close with no Activity. The failed task recorded no intent, so it
    # closed nothing, and the retried task appends again before it closes the Stream.
    records = await _read_all(stream_client, stream.ref.id)
    assert [record.body for record in records] == ["a", "a"]
    assert await _describe_status(stream_client, stream.ref.id) == (
        StreamStatus.StreamStatusClosed
    )


@workflow.defn
class TwoAppendsWorkflow:
    @workflow.run
    async def run(self, stream_id: str, sleep_between: bool) -> None:
        stream = await create_stream(id=stream_id, body_type=str)
        await stream.append("a")
        if sleep_between:
            # A timer ends the Workflow Task, so the next append runs in a later one.
            await workflow.sleep(timedelta(milliseconds=10))
        await stream.append("b")


@workflow.defn
class GatheredAppendsWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        stream = await create_stream(id=stream_id, body_type=str)
        await asyncio.gather(stream.append("a"), stream.append("b"), stream.append("c"))


@workflow.defn
class GatheredAppendAndCloseWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        stream = await create_stream(id=stream_id, body_type=str)
        await asyncio.gather(stream.append("a"), stream.close())


async def _run_two_appends(
    client: Client, store: StreamStore, sleep_between: bool
) -> tuple[str, list[StreamRecord[str]]]:
    stream_client = _with_store(client, store)
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(stream_client, TwoAppendsWorkflow) as worker:
        handle = await stream_client.start_workflow(
            TwoAppendsWorkflow.run,
            args=[stream_id, sleep_between],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()
    assert handle.result_run_id
    return handle.result_run_id, await _read_all(stream_client, stream_id)


async def test_workflow_appends_in_one_task_write_as_one_producer(client: Client):
    run_id, records = await _run_two_appends(
        client, MemoryStreamStore(), sleep_between=False
    )
    assert [record.body for record in records] == ["a", "b"]
    first, second = _producers(records)
    assert first == second
    assert first.id == f"wf:{run_id}"


async def test_workflow_append_in_a_later_task_has_a_higher_epoch(client: Client):
    run_id, records = await _run_two_appends(
        client, MemoryStreamStore(), sleep_between=True
    )
    first, second = _producers(records)
    assert first.id == second.id == f"wf:{run_id}"
    assert _epoch(second) > _epoch(first)


async def test_workflow_appends_started_together_write_in_call_order(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(stream_client, GatheredAppendsWorkflow) as worker:
        await stream_client.execute_workflow(
            GatheredAppendsWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    # Core runs the appends of one Stream one at a time, in the order of the calls.
    records = await _read_all(stream_client, stream_id)
    assert [record.body for record in records] == ["a", "b", "c"]


async def test_workflow_close_started_with_an_append_comes_after_it(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(stream_client, GatheredAppendAndCloseWorkflow) as worker:
        await stream_client.execute_workflow(
            GatheredAppendAndCloseWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    records = await _read_all(stream_client, stream_id)
    assert [record.body for record in records] == ["a"]
    assert await _describe_status(stream_client, stream_id) == (
        StreamStatus.StreamStatusClosed
    )
