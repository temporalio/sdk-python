from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.nexus.system import TEMPORAL_SYSTEM_ENDPOINT
from temporalio.nexus.system.stream_service import (
    Producer,
    ProducerKindLocalActivity,
    ReadRecordsRequest,
    StreamRecord,
    StreamRef,
    StreamService,
)
from temporalio.nexus.system.stream_service.workflow import create_stream, get_stream
from temporalio.testing import MemoryStreamStore
from tests.helpers import assert_task_fail_eventually, new_worker
from tests.helpers.streams import RecordingStreamStore
from tests.nexus.test_stream_store import _with_store

_BODIES = ["first-record-body", "second-record-body"]


@workflow.defn
class StreamCallWorkflow:
    @workflow.run
    async def run(self, operation: str) -> None:
        stream = get_stream(StreamRef(id="stream"), body_type=str)
        if operation == "CreateStream":
            await create_stream(id="stream", body_type=str)
        elif operation == "AppendRecords":
            await stream.append("a")
        elif operation == "CloseStream":
            await stream.close()
        elif operation == "DescribeStream":
            await stream.describe()
        else:
            # The Workflow `Stream` has no `read`, but a Nexus client can start one.
            nexus_client = workflow.create_nexus_client(
                service=StreamService, endpoint=TEMPORAL_SYSTEM_ENDPOINT
            )
            await nexus_client.start_operation(
                operation, ReadRecordsRequest(ref=StreamRef[str](id="stream"))
            )


@pytest.mark.parametrize(
    "operation",
    ["CreateStream", "AppendRecords", "CloseStream", "DescribeStream", "ReadRecords"],
)
async def test_workflow_stream_call_fails_the_workflow_task(
    client: Client, operation: str
):
    store = RecordingStreamStore(MemoryStreamStore())
    stream_client = _with_store(client, store)
    async with new_worker(stream_client, StreamCallWorkflow) as worker:
        handle = await stream_client.start_workflow(
            StreamCallWorkflow.run,
            operation,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        await assert_task_fail_eventually(
            handle,
            message_contains=(
                "Workflow code can't call a Stream in this version. Call it from an "
                "Activity through activity.client()."
            ),
        )
        await handle.terminate()
    # The Store of the Worker's client gets no call.
    assert store.operations == []


@activity.defn
async def append_values(ref: StreamRef[str], values: list[str]) -> None:
    await activity.client().get_stream(ref, body_type=str).append(*values)


_fail_next_task = True


@workflow.defn(sandboxed=False)
class AppendThenFailTaskOnceWorkflow:
    @workflow.run
    async def run(self, ref: StreamRef[str]) -> None:
        await workflow.execute_local_activity(
            append_values,
            args=[ref, _BODIES],
            start_to_close_timeout=timedelta(seconds=10),
        )
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


async def _read_all(client: Client, stream_id: str) -> list[StreamRecord[str]]:
    stream = client.get_stream(StreamRef(id=stream_id), body_type=str)
    return [record async for record in stream.read()]


def _local_activity_epoch(producer: Producer) -> tuple[int, int, int]:
    # The Workflow Task, then the attempt of the Local Activity.
    assert isinstance(producer.kind, ProducerKindLocalActivity)
    local = producer.kind.value.epoch
    return (local.task.started_event_id, local.task.attempt, local.attempt)


def _producers(records: list[StreamRecord[str]]) -> list[Producer]:
    producers = [record.producer for record in records]
    assert all(producer is not None for producer in producers)
    return [producer for producer in producers if producer is not None]


async def test_local_activity_append_after_a_failed_workflow_task_has_a_higher_epoch(
    client: Client,
):
    global _fail_next_task
    _fail_next_task = True
    stream_client = _with_store(client, MemoryStreamStore())
    stream = await stream_client.create_stream(
        id=f"stream-{uuid.uuid4()}", body_type=str
    )
    async with new_worker(
        stream_client, AppendThenFailTaskOnceWorkflow, activities=[append_values]
    ) as worker:
        await stream_client.execute_workflow(
            AppendThenFailTaskOnceWorkflow.run,
            stream.ref,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    records = await _read_all(stream_client, stream.ref.id)
    # The failed Workflow Task records no marker, so its retry runs the Local Activity
    # again and writes a second copy.
    assert [record.body for record in records] == [*_BODIES, *_BODIES]
    producers = _producers(records)
    assert len({producer.id for producer in producers}) == 1
    first, second, third, fourth = (
        _local_activity_epoch(producer) for producer in producers
    )
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
