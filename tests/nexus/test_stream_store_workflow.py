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
    WorkflowHandle,
)
from temporalio.common import RetryPolicy
from temporalio.converter import DataConverter, StorageDriverWorkflowInfo
from temporalio.exceptions import ApplicationError, StreamError
from temporalio.nexus.system import TEMPORAL_SYSTEM_ENDPOINT
from temporalio.nexus.system.stream_service import (
    AppendRecord,
    AppendRecordsRequest,
    ClientProducer,
    CloseStreamRequest,
    Producer,
    ProducerKindClient,
    ProducerKindLocalActivity,
    ReadRecordsRequest,
    StreamIdConflictPolicy,
    StreamRecord,
    StreamRef,
    StreamService,
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
        await _append_in_local_activity(stream.ref, _BODIES)
        return stream.ref


@workflow.defn
class RefusedOperationWorkflow:
    @workflow.run
    async def run(self, operation: str) -> str:
        # The Workflow `Stream` has no method for these operations, so the test starts
        # them through a Nexus client, as Workflow code still can.
        ref = StreamRef[str](id="stream")
        producer = Producer(id="workflow", kind=ProducerKindClient(ClientProducer()))
        requests: dict[str, Any] = {
            "AppendRecords": AppendRecordsRequest(
                ref=ref, records=[AppendRecord(body="a")], producer=producer
            ),
            "CloseStream": CloseStreamRequest(ref=ref, producer=producer),
            "ReadRecords": ReadRecordsRequest(ref=ref),
        }
        nexus_client = workflow.create_nexus_client(
            service=StreamService, endpoint=TEMPORAL_SYSTEM_ENDPOINT
        )
        try:
            await nexus_client.start_operation(operation, requests[operation])
        except ApplicationError as err:
            assert err.non_retryable
            return err.message
        return "started"


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
    async def run(self, stream_id: str) -> None:
        stream = await create_stream(
            id=stream_id,
            body_type=str,
            id_conflict_policy=StreamIdConflictPolicy.StreamIdConflictPolicyUseExisting,
        )
        await _append_in_local_activity(stream.ref, _BODIES)
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


class RecordingInterceptor(Interceptor):
    def __init__(self) -> None:
        self.activity_types: list[str] = []

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _RecordingActivityInbound(next, self.activity_types)


class _RecordingActivityInbound(ActivityInboundInterceptor):
    def __init__(
        self, next: ActivityInboundInterceptor, activity_types: list[str]
    ) -> None:
        super().__init__(next)
        self._activity_types = activity_types

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        self._activity_types.append(activity.info().activity_type)
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
    return [record async for record in stream.iter_read_items()]


def _producers(records: list[StreamRecord[str]]) -> list[Producer]:
    producers = [record.producer for record in records]
    assert all(producer is not None for producer in producers)
    return [producer for producer in producers if producer is not None]


async def _run_append_workflow(
    client: Client,
) -> tuple[WorkflowHandle[AppendWorkflow, StreamRef[str]], StreamRef[str]]:
    async with new_worker(client, AppendWorkflow, activities=[append_values]) as worker:
        handle = await client.start_workflow(
            AppendWorkflow.run,
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


async def test_workflow_stream_uses_store(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    handle, ref = await _run_append_workflow(stream_client)

    stream = stream_client.get_stream(ref, body_type=str)
    records = [record async for record in stream.iter_read_items()]
    assert [record.body for record in records] == _BODIES
    run_id = handle.result_run_id
    assert run_id
    first, second = _producers(records)
    assert first == second
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


async def test_workflow_stream_store_uses_payload_codec(client: Client):
    store = MemoryStreamStore()
    config = client.config()
    config["data_converter"] = DataConverter(payload_codec=SimpleCodec())
    config["stream_config"] = StreamConfig(stores=[store])
    stream_client = Client(**config)
    _, ref = await _run_append_workflow(stream_client)

    # A client without the codec reads the bodies as the Store keeps them.
    raw = _with_store(client, store).get_stream(
        StreamRef(id=ref.id), body_type=temporalio.common.RawValue
    )
    stored = [record.body.payload async for record in raw.iter_read_items()]
    assert [body.metadata["simple-codec"] for body in stored] == [b"true", b"true"]
    stream = stream_client.get_stream(ref, body_type=str)
    assert [record.body async for record in stream.iter_read_items()] == _BODIES


async def test_workflow_stream_history_has_no_bodies(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    handle, _ = await _run_append_workflow(stream_client)
    events = (await handle.fetch_history()).events
    markers = [
        event.marker_recorded_event_attributes.marker_name
        for event in events
        if event.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    ]
    # One marker for the create and one for the Local Activity that appends.
    assert markers == ["core_local_activity", "core_local_activity"]
    for event in events:
        for body in _BODIES:
            assert body.encode() not in event.SerializeToString()


async def test_workflow_stream_replay_does_not_call_store(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    handle, _ = await _run_append_workflow(stream_client)
    replay_store = CountingStore("memory")
    await Replayer(
        workflows=[AppendWorkflow],
        stream_config=StreamConfig(stores=[replay_store]),
    ).replay_workflow(await handle.fetch_history())
    assert replay_store.operations == []


async def test_workflow_create_without_store_fails_the_task(client: Client):
    async with new_worker(client, CreateAndDescribeWorkflow) as worker:
        handle = await client.start_workflow(
            CreateAndDescribeWorkflow.run,
            f"stream-{uuid.uuid4()}",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        await assert_task_fail_eventually(
            handle,
            message_contains=(
                "The client of this Worker has no Stream Store. Give one in "
                "stream_config=StreamConfig(stores=[...])."
            ),
        )
        await handle.terminate()


@pytest.mark.parametrize(
    "operation, message",
    [
        (
            "AppendRecords",
            "Workflow code can't append to a Stream on a Store in this release. "
            "Call a Local Activity that appends through its client.",
        ),
        (
            "CloseStream",
            "Workflow code can't close a Stream on a Store in this release. "
            "Call an Activity that closes it through its client.",
        ),
        (
            "ReadRecords",
            "Workflow code can't read a Stream on a Store in this release. "
            "Call an Activity that reads it through its client.",
        ),
    ],
)
async def test_workflow_stream_operation_fails_with_store(
    client: Client, operation: str, message: str
):
    stream_client = _with_store(client, MemoryStreamStore())
    async with new_worker(stream_client, RefusedOperationWorkflow) as worker:
        result = await stream_client.execute_workflow(
            RefusedOperationWorkflow.run,
            operation,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert result == message


def test_workflow_stream_has_no_append_close_or_read():
    for name in ("append", "close", "read"):
        assert not hasattr(WorkflowStream, name)
    assert hasattr(WorkflowStream, "describe")


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


async def test_client_append_after_local_activity_append_keeps_offset_order(
    client: Client,
):
    stream_client = _with_store(client, MemoryStreamStore())
    _, ref = await _run_append_workflow(stream_client)
    stream = stream_client.get_stream(ref, body_type=str)
    appended = await stream.append("from-client")
    records = await _read_all(stream_client, ref.id)
    assert [record.body for record in records] == [*_BODIES, "from-client"]
    offsets = [record.offset for record in records]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)
    assert offsets[-1] == appended.last_offset
    kinds = [type(producer.kind) for producer in _producers(records)]
    assert kinds == [
        ProducerKindLocalActivity,
        ProducerKindLocalActivity,
        ProducerKindClient,
    ]


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


async def test_workflow_task_retry_appends_with_higher_epoch(client: Client):
    global _fail_next_task
    _fail_next_task = True
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(
        stream_client, AppendThenFailTaskOnceWorkflow, activities=[append_values]
    ) as worker:
        await stream_client.execute_workflow(
            AppendThenFailTaskOnceWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    records = await _read_all(stream_client, stream_id)
    # The failed Workflow Task records no marker, so its retry runs the Local Activity
    # again and writes a second copy.
    assert [record.body for record in records] == [*_BODIES, *_BODIES]
    producers = _producers(records)
    assert len({producer.id for producer in producers}) == 1

    def epoch(producer: Producer) -> tuple[int, int, int]:
        assert isinstance(producer.kind, ProducerKindLocalActivity)
        local = producer.kind.value.epoch
        return (local.task.started_event_id, local.task.attempt, local.attempt)

    first, second, third, fourth = (epoch(producer) for producer in producers)
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
