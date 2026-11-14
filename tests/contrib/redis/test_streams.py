from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Awaitable
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.api.enums.v1 import StreamErrorReason
from temporalio.client import Client, StreamConfig, StreamStore
from temporalio.contrib.redis.streams import RedisStreamStore
from temporalio.exceptions import StreamError
from temporalio.nexus.system.stream_service import (
    ClientProducer,
    Producer,
    ProducerKindClient,
    ProducerKindLocalActivity,
    ProducerKindWorkflowActivity,
    StreamRef,
    StreamStatus,
)
from temporalio.nexus.system.stream_service.client import Stream
from temporalio.nexus.system.stream_service.workflow import create_stream
from tests.helpers import new_worker

_REDIS_URL = os.environ.get("STREAMS_REDIS_URL")

pytestmark = pytest.mark.skipif(
    not _REDIS_URL, reason="STREAMS_REDIS_URL is not set to a Redis server"
)


def _store() -> RedisStreamStore:
    assert _REDIS_URL
    # Each test has its own key prefix, so that runs on one Redis don't share Streams.
    return RedisStreamStore(_REDIS_URL, key_prefix=f"test-{uuid.uuid4()}")


def _with_store(client: Client, store: StreamStore) -> Client:
    config = client.config()
    config["stream_config"] = StreamConfig(stores=[store])
    return Client(**config)


async def _create_stream(client: Client) -> Stream[str]:
    return await client.create_stream(id=f"stream-{uuid.uuid4()}", body_type=str)


async def _stream_error(call: Awaitable[object]) -> StreamError:
    with pytest.raises(StreamError) as err:
        await call
    return err.value


@activity.defn
async def append_values(ref: StreamRef[str], values: list[str]) -> None:
    await activity.client().get_stream(ref, body_type=str).append(*values)


@workflow.defn
class AppendWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> StreamRef[str]:
        stream = await create_stream(id=stream_id, body_type=str)
        await workflow.execute_local_activity(
            append_values,
            args=[stream.ref, ["a", "b"]],
            start_to_close_timeout=timedelta(seconds=10),
        )
        return stream.ref


@workflow.defn
class AppendAndCloseWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> StreamRef[str]:
        stream = await create_stream(id=stream_id, body_type=str)
        await stream.append("a", "b")
        await stream.close()
        return stream.ref


@activity.defn
async def append_in_activity(stream_id: str) -> None:
    stream = activity.client().get_stream(StreamRef(id=stream_id), body_type=str)
    await stream.append("from-activity")


@workflow.defn
class RunActivityWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> None:
        await workflow.execute_activity(
            append_in_activity,
            stream_id,
            start_to_close_timeout=timedelta(seconds=10),
        )


async def test_client_creates_appends_reads_and_closes(client: Client):
    stream_client = _with_store(client, _store())
    stream = await _create_stream(stream_client)
    assert stream.created
    assert stream.ref.store == "redis"

    appended = await stream.append("a", "b")
    assert appended.first_offset is not None
    assert appended.last_offset is not None
    records = [record async for record in stream.iter_read_items()]
    assert [(record.body, record.offset) for record in records] == [
        ("a", appended.first_offset),
        ("b", appended.last_offset),
    ]

    await stream.close()
    described = await stream.describe()
    assert described.status == StreamStatus.StreamStatusClosed
    assert described.last_offset == appended.last_offset
    page = await stream.read(from_offset=appended.last_offset)
    assert page.done
    assert page.records == []


async def test_workflow_local_activity_appends_with_local_activity_producer(
    client: Client,
):
    stream_client = _with_store(client, _store())
    async with new_worker(
        stream_client, AppendWorkflow, activities=[append_values]
    ) as worker:
        handle = await stream_client.start_workflow(
            AppendWorkflow.run,
            f"stream-{uuid.uuid4()}",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        ref = await handle.result()

    stream = stream_client.get_stream(ref, body_type=str)
    records = [record async for record in stream.iter_read_items()]
    assert [record.body for record in records] == ["a", "b"]
    run_id = handle.result_run_id
    for record in records:
        assert record.producer is not None
        assert isinstance(record.producer.kind, ProducerKindLocalActivity)
        assert record.producer.kind.value.run_id == run_id


async def test_workflow_appends_and_closes(client: Client):
    stream_client = _with_store(client, _store())
    async with new_worker(stream_client, AppendAndCloseWorkflow) as worker:
        handle = await stream_client.start_workflow(
            AppendAndCloseWorkflow.run,
            f"stream-{uuid.uuid4()}",
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        ref = await handle.result()

    stream = stream_client.get_stream(ref, body_type=str)
    records = [record async for record in stream.iter_read_items()]
    assert [record.body for record in records] == ["a", "b"]
    run_id = handle.result_run_id
    for record in records:
        assert record.producer is not None
        assert isinstance(record.producer.kind, ProducerKindLocalActivity)
        assert record.producer.kind.value.run_id == run_id
    described = await stream.describe()
    assert described.status == StreamStatus.StreamStatusClosed
    err = await _stream_error(stream.append("c"))
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_CLOSED


async def test_activity_appends_with_activity_producer(client: Client):
    stream_client = _with_store(client, _store())
    stream = await _create_stream(stream_client)
    async with new_worker(
        stream_client, RunActivityWorkflow, activities=[append_in_activity]
    ) as worker:
        handle = await stream_client.start_workflow(
            RunActivityWorkflow.run,
            stream.ref.id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        await handle.result()

    [record] = [record async for record in stream.iter_read_items()]
    assert record.body == "from-activity"
    assert record.producer is not None
    assert isinstance(record.producer.kind, ProducerKindWorkflowActivity)
    assert record.producer.kind.value.run_id == handle.result_run_id


async def test_client_append_with_sequence_writes_once(client: Client):
    stream = await _create_stream(_with_store(client, _store()))
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


async def test_failed_calls_raise_stream_error_with_reason(client: Client):
    stream_client = _with_store(client, _store())
    stream = await _create_stream(stream_client)

    err = await _stream_error(
        stream_client.create_stream(id=stream.ref.id, body_type=str)
    )
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_ALREADY_EXISTS

    unknown = stream_client.get_stream(
        StreamRef(id=f"stream-{uuid.uuid4()}"), body_type=str
    )
    err = await _stream_error(unknown.describe())
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND

    await stream.close()
    err = await _stream_error(stream.append("a"))
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_CLOSED


async def test_read_with_wait_gets_a_later_record(client: Client):
    stream_client = _with_store(client, _store())
    stream = await _create_stream(stream_client)

    async def append_later() -> None:
        await asyncio.sleep(0.5)
        await stream.append("late")

    append = asyncio.create_task(append_later())
    page = await stream.read(wait=timedelta(seconds=10))
    await append
    assert [record.body for record in page.records] == ["late"]


async def test_key_prefixes_keep_streams_apart(client: Client):
    first = _with_store(client, _store())
    second = _with_store(client, _store())
    stream = await _create_stream(first)
    await stream.append("a")

    other = second.get_stream(StreamRef(id=stream.ref.id), body_type=str)
    err = await _stream_error(other.describe())
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_NOT_FOUND
    created = await second.create_stream(id=stream.ref.id, body_type=str)
    assert created.created


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_store_without_server_fails_at_first_call(client: Client):
    store = RedisStreamStore(f"redis://127.0.0.1:{_free_port()}")
    stream_client = _with_store(client, store)
    err = await _stream_error(_create_stream(stream_client))
    assert err.reason == StreamErrorReason.STREAM_ERROR_REASON_STORAGE_UNAVAILABLE
    assert not err.non_retryable


async def test_bad_option_fails_with_the_crate_message(client: Client):
    assert _REDIS_URL
    store = RedisStreamStore([_REDIS_URL, _REDIS_URL])
    with pytest.raises(ValueError, match="cluster"):
        await _create_stream(_with_store(client, store))
