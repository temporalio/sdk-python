from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import google.protobuf.timestamp_pb2
import pytest

import temporalio
import temporalio.activity
import temporalio.api.stream.v1
import temporalio.api.workflowservice.v1
import temporalio.client._stream_store
import temporalio.common
import temporalio.converter._payload_converter
import temporalio.exceptions
import temporalio.nexus.system
import temporalio.nexus.system._support
import temporalio.workflow
from temporalio.api.enums.v1 import StreamErrorReason
from temporalio.client import (
    Client,
    StreamConfig,
    StreamSelectContext,
    StreamStore,
)
from temporalio.client._stream_store import _STREAM_OPERATIONS, _request_ref
from temporalio.converter import DataConverter, StorageDriverActivityInfo
from temporalio.exceptions import ApplicationError, StreamConfigError, StreamError
from temporalio.nexus.system.stream_service import (
    ClientProducer,
    DescribeStreamRequest,
    Producer,
    ProducerKindClient,
    ProducerKindLocalActivity,
    ProducerKindStandaloneActivity,
    ProducerKindWorkflowActivity,
    StreamIdConflictPolicy,
    StreamRef,
    StreamService,
    StreamStatus,
    Timestamp,
)
from temporalio.nexus.system.stream_service.client import (
    Stream,
    StreamServiceClientExtension,
)
from temporalio.testing import (
    ActivityEnvironment,
    MemoryStreamStore,
    WorkflowEnvironment,
)
from tests import DEV_SERVER_DOWNLOAD_VERSION
from tests.helpers.streams import DictStreamStore
from tests.test_converter import SimpleCodec


def _with_store(client: Client, store: StreamStore) -> Client:
    return _with_config(client, StreamConfig(stores=[store]))


def _with_config(client: Client, stream_config: StreamConfig) -> Client:
    config = client.config()
    config["stream_config"] = stream_config
    return Client(**config)


def _two_stores(
    selector: Callable[[StreamSelectContext], StreamStore],
) -> tuple[MemoryStreamStore, MemoryStreamStore, StreamConfig]:
    first = MemoryStreamStore(name="first")
    second = MemoryStreamStore(name="second")
    return first, second, StreamConfig(stores=[first, second], store_selector=selector)


async def _create_stream(client: Client, store: StreamStore) -> Stream[str]:
    return await _with_store(client, store).create_stream(
        id=f"stream-{uuid.uuid4()}", body_type=str
    )


async def _only_producer(stream: Stream[str]) -> Producer:
    [record] = [record async for record in stream.iter_read_items()]
    assert record.producer is not None
    return record.producer


async def _append_in_activity(
    client: Client, info: temporalio.activity.Info
) -> Producer:
    stream = await _create_stream(client, MemoryStreamStore())
    env = ActivityEnvironment()
    env.info = info
    await env.run(stream.append, "a")
    return await _only_producer(stream)


def _timestamp(value: datetime) -> Timestamp:
    proto = google.protobuf.timestamp_pb2.Timestamp()
    proto.FromDatetime(value)
    return Timestamp(seconds=proto.seconds, nanos=proto.nanos)


async def _stream_error(call: Awaitable[object]) -> StreamError:
    with pytest.raises(StreamError) as err:
        await call
    return err.value


async def test_client_stream_uses_store(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())

    stream = await stream_client.create_stream(
        id=f"stream-{uuid.uuid4()}", body_type=str
    )
    assert stream.created
    assert stream.ref.store == "memory"
    appended = await stream.append("a", "b")
    assert appended.first_offset is not None
    assert appended.last_offset is not None
    assert appended.first_offset < appended.last_offset
    records = [record async for record in stream.iter_read_items()]
    assert [(record.body, record.offset) for record in records] == [
        ("a", appended.first_offset),
        ("b", appended.last_offset),
    ]

    other = stream_client.get_stream(stream.ref, body_type=str)
    assert [record.body async for record in other.iter_read_items()] == ["a", "b"]

    described = await stream.describe()
    assert described.status == StreamStatus.StreamStatusOpen
    assert described.last_offset == appended.last_offset

    await stream.close()
    described = await stream.describe()
    assert described.status == StreamStatus.StreamStatusClosed
    assert described.last_offset == appended.last_offset
    page = await stream.read(from_offset=records[-1].offset)
    assert page.done
    assert page.records == []
    pages = [page async for page in stream.iter_read()]
    assert pages[-1].done
    assert [record.body for page in pages for record in page.records] == ["a", "b"]


async def test_client_stream_uses_a_store_written_from_scratch(client: Client):
    store = DictStreamStore()
    config = client.config()
    config["data_converter"] = DataConverter(payload_codec=SimpleCodec())
    config["stream_config"] = StreamConfig(stores=[store])
    stream_client = Client(**config)

    stream_id = f"stream-{uuid.uuid4()}"
    stream = await stream_client.create_stream(id=stream_id, body_type=str)
    assert stream.created
    assert stream.ref.store == "dict"
    appended = await stream.append("a", "b")
    assert [record.body async for record in stream.iter_read_items()] == ["a", "b"]
    # The Store gets payloads that the client already encoded with the codec.
    stored = store.streams[stream_id].records
    assert [record.body.metadata["simple-codec"] for record in stored] == [
        b"true",
        b"true",
    ]
    described = await stream.describe()
    assert described.status == StreamStatus.StreamStatusOpen
    assert described.last_offset == appended.last_offset
    err = await _stream_error(stream_client.create_stream(id=stream_id, body_type=str))
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_ALREADY_EXISTS

    await stream.close()
    err = await _stream_error(stream.append("c"))
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_CLOSED


def test_stream_operations_match_the_stream_service():
    service = temporalio.client._stream_store._STREAM_SERVICE
    assert set(_STREAM_OPERATIONS) == set(service.operation_definitions)
    for name, operation in _STREAM_OPERATIONS.items():
        definition = service.operation_definitions[name]
        for model, message in (
            (definition.input_type, operation.request_type),
            (definition.output_type, operation.response_type),
        ):
            converter = (
                temporalio.converter._payload_converter._get_transfer_type_converter(
                    model
                )
            )
            assert converter is not None
            assert converter.transfer_type is message
        assert callable(getattr(StreamStore, operation.store_method))


async def test_client_strict_create_of_existing_stream_fails(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    await stream_client.create_stream(id=stream_id, body_type=str)
    err = await _stream_error(stream_client.create_stream(id=stream_id, body_type=str))
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_ALREADY_EXISTS
    assert err.non_retryable
    existing = await stream_client.create_stream(
        id=stream_id,
        body_type=str,
        id_conflict_policy=StreamIdConflictPolicy.StreamIdConflictPolicyUseExisting,
    )
    # The response's `created` is false, which the generated model reads as unset.
    assert not existing.created


async def test_client_read_or_describe_of_unknown_stream_fails(client: Client):
    stream_client = _with_store(client, MemoryStreamStore())
    stream = stream_client.get_stream(
        StreamRef(id=f"stream-{uuid.uuid4()}"), body_type=str
    )
    for call in (stream.read(), stream.describe()):
        err = await _stream_error(call)
        assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND


async def test_client_append_after_close_fails(client: Client):
    stream = await _create_stream(client, MemoryStreamStore())
    await stream.append("a")
    await stream.close()
    err = await _stream_error(stream.append("b"))
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_CLOSED
    assert err.type == "StreamError"


async def test_client_stream_store_uses_payload_codec(client: Client):
    store = MemoryStreamStore()
    config = client.config()
    config["data_converter"] = DataConverter(payload_codec=SimpleCodec())
    config["stream_config"] = StreamConfig(stores=[store])
    stream_client = Client(**config)

    stream = await stream_client.create_stream(
        id=f"stream-{uuid.uuid4()}", body_type=str
    )
    await stream.append("a", "b")
    # A client without the codec reads the bodies as the Store keeps them.
    raw = _with_store(client, store).get_stream(
        StreamRef(id=stream.ref.id), body_type=temporalio.common.RawValue
    )
    stored = [record.body.payload async for record in raw.iter_read_items()]
    assert [body.metadata["simple-codec"] for body in stored] == [b"true", b"true"]
    assert [record.body async for record in stream.iter_read_items()] == ["a", "b"]


async def test_client_append_has_client_producer(client: Client):
    stream = await _create_stream(client, MemoryStreamStore())
    await stream.append("a")
    producer = await _only_producer(stream)
    assert producer.id.startswith("client:")
    assert isinstance(producer.kind, ProducerKindClient)


async def test_workflow_activity_append_has_activity_producer(client: Client):
    scheduled = datetime(2026, 11, 12, 10, 0, 0, 123456, tzinfo=timezone.utc)
    attempt_scheduled = scheduled + timedelta(seconds=30)
    producer = await _append_in_activity(
        client,
        dataclasses.replace(
            ActivityEnvironment.default_info(),
            activity_id="step:1",
            attempt=3,
            workflow_run_id="run-1",
            scheduled_time=scheduled,
            current_attempt_scheduled_time=attempt_scheduled,
        ),
    )
    epoch_nanos = (
        (scheduled - datetime(1970, 1, 1, tzinfo=timezone.utc))
        // timedelta(microseconds=1)
        * 1000
    )
    assert producer.id == f"act:run-1:step%3A1:{epoch_nanos}"
    assert isinstance(producer.kind, ProducerKindWorkflowActivity)
    assert producer.kind.value.run_id == "run-1"
    assert producer.kind.value.epoch.attempt == 3
    assert producer.kind.value.epoch.attempt_scheduled_time == _timestamp(
        attempt_scheduled
    )


async def test_standalone_activity_append_has_activity_producer(client: Client):
    attempt_scheduled = datetime(2026, 11, 12, 10, 0, 30, tzinfo=timezone.utc)
    producer = await _append_in_activity(
        client,
        dataclasses.replace(
            ActivityEnvironment.default_info(),
            activity_id="job",
            activity_run_id="job-run",
            attempt=2,
            workflow_id=None,
            workflow_namespace=None,
            workflow_run_id=None,
            workflow_type=None,
            current_attempt_scheduled_time=attempt_scheduled,
        ),
    )
    assert producer.id == "sa:job:job-run"
    assert isinstance(producer.kind, ProducerKindStandaloneActivity)
    assert not producer.kind.value.run_id
    assert producer.kind.value.epoch.attempt == 2
    assert producer.kind.value.epoch.attempt_scheduled_time == _timestamp(
        attempt_scheduled
    )


async def test_local_activity_append_has_local_activity_producer(client: Client):
    task_started = datetime(2026, 11, 12, 10, 0, 0, 123456, tzinfo=timezone.utc)
    producer = await _append_in_activity(
        client,
        dataclasses.replace(
            ActivityEnvironment.default_info(),
            activity_id="3",
            attempt=2,
            is_local=True,
            workflow_run_id="run:1",
            workflow_task=temporalio.activity.WorkflowTaskAttempt(
                started_event_id=7, attempt=1, started_time=task_started
            ),
        ),
    )
    assert producer.id == "la:run%3A1:3"
    assert isinstance(producer.kind, ProducerKindLocalActivity)
    assert producer.kind.value.run_id == "run:1"
    assert producer.kind.value.epoch.task.started_event_id == 7
    assert producer.kind.value.epoch.task.attempt == 1
    assert producer.kind.value.epoch.task.started_time == _timestamp(task_started)
    assert producer.kind.value.epoch.attempt == 2


async def test_local_activity_append_without_workflow_task_fails(client: Client):
    with pytest.raises(RuntimeError, match="Workflow Task"):
        await _append_in_activity(
            client,
            dataclasses.replace(ActivityEnvironment.default_info(), is_local=True),
        )


async def test_explicit_producer_is_sent(client: Client):
    stream = await _create_stream(client, MemoryStreamStore())
    with mock.patch.object(
        temporalio.nexus.system._support, "default_producer"
    ) as default_producer:
        await stream.append(
            "a", producer=Producer(id="mine", kind=ProducerKindClient(ClientProducer()))
        )
    default_producer.assert_not_called()
    producer = await _only_producer(stream)
    assert producer.id == "mine"
    assert isinstance(producer.kind, ProducerKindClient)


async def test_client_append_with_sequence_writes_once(client: Client):
    stream = await _create_stream(client, MemoryStreamStore())
    producer = Producer(id="mine", kind=ProducerKindClient(ClientProducer()))
    first = await stream.append("a", "b", producer=producer, sequence=5)
    second = await stream.append("a", "b", producer=producer, sequence=5)
    assert second == first
    records = [record async for record in stream.iter_read_items()]
    assert [record.body for record in records] == ["a", "b"]
    assert [record.offset for record in records] == [
        first.first_offset,
        first.last_offset,
    ]


def test_client_stream_methods_are_generated():
    assert "create_stream" not in vars(Client)
    assert "get_stream" not in vars(Client)
    assert Client.create_stream is StreamServiceClientExtension.create_stream
    assert Client.get_stream is StreamServiceClientExtension.get_stream


def test_no_hand_written_stream_classes():
    root = Path(temporalio.__file__).parent
    generated = root / "nexus" / "system" / "stream_service"
    names = {"Stream", "WorkflowStream", "StreamCalls", "StreamClient"}
    found: list[tuple[str, str]] = []
    for source in sorted(root.rglob("*.py")):
        if generated in source.parents:
            continue
        for node in ast.walk(ast.parse(source.read_text(encoding="utf8"))):
            if isinstance(node, ast.ClassDef) and node.name in names:
                found.append((source.relative_to(root).as_posix(), node.name))
    # The Workflow Streams contrib library is older than the generated Stream API and
    # doesn't use it.
    found.remove(("contrib/workflow_streams/_stream.py", "WorkflowStream"))
    assert found == []
    for name in ("Stream", "WorkflowStream"):
        value = getattr(temporalio.workflow, name, None)
        assert value is None or value.__module__.startswith(
            "temporalio.nexus.system.stream_service."
        )


def test_each_stream_module_can_be_the_first_import():
    modules = [
        "temporalio.nexus.system.stream_service.workflow",
        "temporalio.nexus.system.stream_service.client",
        "temporalio.nexus.system.stream_service",
        "temporalio.nexus.system",
        "temporalio.workflow",
        "temporalio.client",
        "temporalio.worker",
        "temporalio.contrib.redis.streams",
    ]
    failures: dict[str, str] = {}
    for module in modules:
        # Only a new interpreter has no module loaded, so the import order is real.
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            failures[module] = result.stderr.strip().splitlines()[-1]
    assert failures == {}


async def test_client_without_stream_store_raises_and_sends_nothing(client: Client):
    requests: list[
        temporalio.api.workflowservice.v1.StartNexusOperationExecutionRequest
    ] = []

    async def start_nexus_operation_execution(
        req: temporalio.api.workflowservice.v1.StartNexusOperationExecutionRequest,
        **_: object,
    ) -> temporalio.api.workflowservice.v1.StartNexusOperationExecutionResponse:
        requests.append(req)
        return temporalio.api.workflowservice.v1.StartNexusOperationExecutionResponse(
            run_id="run"
        )

    nexus_client = client.create_nexus_client(
        StreamService, temporalio.nexus.system.TEMPORAL_SYSTEM_ENDPOINT
    )
    with mock.patch.object(
        client.workflow_service,
        "start_nexus_operation_execution",
        start_nexus_operation_execution,
    ):
        with pytest.raises(StreamConfigError) as err:
            await nexus_client.start_operation(
                StreamService.describe_stream,
                DescribeStreamRequest(ref=StreamRef(id="stream")),
                id="operation",
            )
        # The generated client method goes the same way.
        with pytest.raises(StreamConfigError):
            await client.create_stream(id="stream", body_type=str)
    assert str(err.value) == (
        "This client has no Stream Store. Give one in "
        "stream_config=StreamConfig(stores=[...])."
    )
    assert requests == []


def test_stream_config_refuses_a_bad_set_of_stores():
    with pytest.raises(StreamConfigError, match="at least one Store"):
        StreamConfig(stores=[])
    same_name = [MemoryStreamStore(name="a"), MemoryStreamStore(name="a")]
    with pytest.raises(StreamConfigError, match="more than one Store named 'a'"):
        StreamConfig(stores=same_name, store_selector=lambda _: same_name[0])
    assert not issubclass(StreamConfigError, StreamError)


def test_stream_config_takes_several_stores_with_no_selector():
    first, second = MemoryStreamStore(name="a"), MemoryStreamStore(name="b")
    stream_config = StreamConfig(stores=[first, second])
    assert stream_config.store_selector is None
    assert stream_config._store_for(StreamRef(id="stream", store="b"), None) is second
    with pytest.raises(StreamConfigError, match="more than one Store"):
        stream_config._store_for(StreamRef(id="stream"), None)


async def test_client_create_with_store_on_several_stores_needs_no_selector(
    client: Client,
):
    first = MemoryStreamStore(name="first")
    second = MemoryStreamStore(name="second")
    stream_client = _with_config(client, StreamConfig(stores=[first, second]))
    stream = await stream_client.create_stream(
        id=f"stream-{uuid.uuid4()}", store="second", body_type=str
    )
    assert stream.ref.store == "second"
    await stream.append("a")
    in_second = _with_store(client, second).get_stream(stream.ref, body_type=str)
    assert [record.body async for record in in_second.iter_read_items()] == ["a"]
    in_first = _with_store(client, first).get_stream(
        StreamRef(id=stream.ref.id), body_type=str
    )
    err = await _stream_error(in_first.describe())
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND


async def test_client_create_without_store_on_several_stores_fails(client: Client):
    stream_config = StreamConfig(
        stores=[MemoryStreamStore(name="first"), MemoryStreamStore(name="second")]
    )
    stream_client = _with_config(client, stream_config)
    with pytest.raises(StreamConfigError) as err:
        await stream_client.create_stream(id="stream", body_type=str)
    assert str(err.value) == (
        "StreamConfig has more than one Store and no store_selector, and the call "
        "on the Stream 'stream' names no Store. Name one with store=, or set "
        "StreamConfig.store_selector."
    )


def test_client_refuses_a_store_in_plugins(client: Client):
    config = client.config()
    config["plugins"] = [MemoryStreamStore()]  # type: ignore[list-item]
    with pytest.raises(StreamConfigError, match="plugins"):
        Client(**config)


async def test_selector_picks_the_store_of_a_new_stream(client: Client):
    contexts: list[StreamSelectContext] = []

    def select(context: StreamSelectContext) -> StreamStore:
        contexts.append(context)
        return second

    first, second, stream_config = _two_stores(select)
    stream_client = _with_config(client, stream_config)
    stream_id = f"stream-{uuid.uuid4()}"
    stream = await stream_client.create_stream(id=stream_id, body_type=str)
    assert stream.ref.store == "second"
    assert contexts == [StreamSelectContext(id=stream_id, target=None)]

    await stream_client.get_stream(stream.ref, body_type=str).append("a")
    # A call through a ref that names its Store doesn't ask the selector.
    assert len(contexts) == 1
    in_second = _with_store(client, second).get_stream(stream.ref, body_type=str)
    assert [record.body async for record in in_second.iter_read_items()] == ["a"]
    in_first = _with_store(client, first).get_stream(
        StreamRef(id=stream_id), body_type=str
    )
    err = await _stream_error(in_first.describe())
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND


async def test_selector_gets_the_activity_that_makes_the_call(client: Client):
    contexts: list[StreamSelectContext] = []

    def select(context: StreamSelectContext) -> StreamStore:
        contexts.append(context)
        return first

    first, _, stream_config = _two_stores(select)
    stream_client = _with_config(client, stream_config)
    stream_id = f"stream-{uuid.uuid4()}"
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        ActivityEnvironment.default_info(),
        activity_id="job",
        activity_type="make_stream",
        activity_run_id="job-run",
        namespace="streams",
    )

    async def create() -> Stream[str]:
        return await stream_client.create_stream(id=stream_id, body_type=str)

    stream = await env.run(create)
    assert stream.ref.store == "first"
    assert contexts == [
        StreamSelectContext(
            id=stream_id,
            target=StorageDriverActivityInfo(
                id="job", type="make_stream", run_id="job-run", namespace="streams"
            ),
        )
    ]


async def test_selector_that_returns_another_store_fails(client: Client):
    _, _, stream_config = _two_stores(lambda _: MemoryStreamStore(name="first"))
    stream_client = _with_config(client, stream_config)
    with pytest.raises(StreamConfigError, match="isn't one of"):
        await stream_client.create_stream(id=f"stream-{uuid.uuid4()}", body_type=str)


async def test_ref_of_a_store_that_the_client_lacks_fails(client: Client):
    stream_client = _with_store(client, MemoryStreamStore(name="first"))
    stream = stream_client.get_stream(
        StreamRef(id=f"stream-{uuid.uuid4()}", store="other"), body_type=str
    )
    with pytest.raises(StreamConfigError, match="'other'"):
        await stream.describe()


def test_request_without_a_stream_ref_fails():
    request = Producer(id="mine", kind=ProducerKindClient(ClientProducer()))
    with pytest.raises(TypeError, match="Producer has no StreamRef"):
        _request_ref(request)


async def test_start_local_gives_its_client_the_stream_config():
    stream_config = StreamConfig(stores=[MemoryStreamStore()])
    async with await WorkflowEnvironment.start_local(
        stream_config=stream_config,
        dev_server_download_version=DEV_SERVER_DOWNLOAD_VERSION,
    ) as env:
        assert env.client.config()["stream_config"] is stream_config
        stream = await env.client.create_stream(
            id=f"stream-{uuid.uuid4()}", body_type=str
        )
        assert stream.ref.store == "memory"


def test_stream_error_carries_its_reason():
    err = StreamError(
        "the Stream is closed", reason=StreamErrorReason.STREAM_ERROR_REASON_CLOSED
    )
    assert err.type == "StreamError"
    assert err.message == "the Stream is closed"
    assert err.non_retryable
    assert list(err.details) == [
        temporalio.api.stream.v1.StreamErrorDetails(
            reason=StreamErrorReason.STREAM_ERROR_REASON_CLOSED
        )
    ]
    assert not StreamError(
        "down", reason=StreamErrorReason.STREAM_ERROR_REASON_STORAGE_UNAVAILABLE
    ).non_retryable
    assert StreamError("x").reason == (
        StreamErrorReason.STREAM_ERROR_REASON_UNSPECIFIED
    )


def test_stream_error_from_application_error():
    from_failure = temporalio.exceptions._stream_error_from_failure
    rebuilt = from_failure(
        ApplicationError(
            "the Stream exists",
            temporalio.api.stream.v1.StreamErrorDetails(
                reason=StreamErrorReason.STREAM_ERROR_REASON_ALREADY_EXISTS
            ),
            type="StreamError",
        )
    )
    assert isinstance(rebuilt, StreamError)
    assert rebuilt.reason == StreamErrorReason.STREAM_ERROR_REASON_ALREADY_EXISTS
    assert rebuilt.message == "the Stream exists"
    unknown = from_failure(
        ApplicationError(
            "x",
            temporalio.api.stream.v1.StreamErrorDetails(
                reason=StreamErrorReason.ValueType(999)
            ),
            type="StreamError",
        )
    )
    assert unknown is not None
    assert unknown.reason == StreamErrorReason.STREAM_ERROR_REASON_UNSPECIFIED
    assert from_failure(ApplicationError("x", type="RuntimeError")) is None
    assert from_failure(RuntimeError("x")) is None
