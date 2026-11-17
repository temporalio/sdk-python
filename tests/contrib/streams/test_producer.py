from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import temporalio.activity
import temporalio.api.stream.v1
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.contrib.streams import as_producer, current_producer
from temporalio.converter import DataConverter
from temporalio.nexus.system.stream_service import (
    Producer,
    ProducerKindClient,
    ProducerKindLocalActivity,
    ProducerKindStandaloneActivity,
    ProducerKindWorkflow,
    ProducerKindWorkflowActivity,
    StreamRecord,
    StreamRef,
    StreamStatus,
    WorkflowProducer,
)
from temporalio.nexus.system.stream_service.client import Stream
from temporalio.nexus.system.stream_service.workflow import create_stream
from temporalio.testing import ActivityEnvironment, MemoryStreamStore
from tests.helpers import new_worker
from tests.nexus.test_stream_store import _create_stream, _with_store

_SCHEDULED = datetime(2026, 11, 12, 10, 0, 0, 123456, tzinfo=timezone.utc)

# One `activity.Info` for each kind of Activity that can write to a Stream.
_ACTIVITY_INFOS = {
    "workflow_activity": dataclasses.replace(
        ActivityEnvironment.default_info(),
        activity_id="step",
        attempt=2,
        workflow_run_id="run-1",
        scheduled_time=_SCHEDULED,
        current_attempt_scheduled_time=_SCHEDULED + timedelta(seconds=30),
    ),
    "standalone_activity": dataclasses.replace(
        ActivityEnvironment.default_info(),
        activity_id="job",
        activity_run_id="job-run",
        attempt=2,
        workflow_id=None,
        workflow_namespace=None,
        workflow_run_id=None,
        workflow_type=None,
        current_attempt_scheduled_time=_SCHEDULED,
    ),
    "local_activity": dataclasses.replace(
        ActivityEnvironment.default_info(),
        activity_id="3",
        attempt=2,
        is_local=True,
        workflow_run_id="run-1",
        workflow_task=temporalio.activity.WorkflowTaskAttempt(
            started_event_id=7, attempt=1, started_time=_SCHEDULED
        ),
    ),
}

_ACTIVITY_KINDS = {
    "workflow_activity": ProducerKindWorkflowActivity,
    "standalone_activity": ProducerKindStandaloneActivity,
    "local_activity": ProducerKindLocalActivity,
}


def _activity_producer(kind: str) -> Producer:
    env = ActivityEnvironment()
    env.info = _ACTIVITY_INFOS[kind]
    return env.run(current_producer)


def _producer_of_each_kind() -> dict[str, Producer]:
    return {
        "workflow": Producer(
            id="wf:run-1", kind=ProducerKindWorkflow(WorkflowProducer(run_id="run-1"))
        ),
        **{kind: _activity_producer(kind) for kind in _ACTIVITY_INFOS},
        "client": current_producer(),
    }


async def _records(stream: Stream[str]) -> list[StreamRecord[str]]:
    return [record async for record in stream.read()]


async def test_current_producer_in_other_code_is_the_client_producer(client: Client):
    stream = await _create_stream(client, MemoryStreamStore())
    await stream.append("a")
    [record] = await _records(stream)
    producer = current_producer()
    assert record.producer == producer
    assert isinstance(producer.kind, ProducerKindClient)


@pytest.mark.parametrize("kind", list(_ACTIVITY_INFOS))
async def test_current_producer_in_an_activity_is_its_attempt_producer(
    client: Client, kind: str
):
    stream = await _create_stream(client, MemoryStreamStore())

    async def append_and_give_producer() -> Producer:
        await stream.append("a")
        return current_producer()

    env = ActivityEnvironment()
    env.info = _ACTIVITY_INFOS[kind]
    producer = await env.run(append_and_give_producer)
    [record] = await _records(stream)
    assert record.producer == producer
    assert isinstance(producer.kind, _ACTIVITY_KINDS[kind])


@workflow.defn
class CurrentProducerWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> Producer:
        stream = await create_stream(id=stream_id, body_type=str)
        producer = current_producer()
        await stream.append("a", producer=producer)
        await stream.close(producer=producer)
        return producer


async def test_current_producer_in_workflow_code_is_the_workflow_producer(
    client: Client,
):
    stream_client = _with_store(client, MemoryStreamStore())
    stream_id = f"stream-{uuid.uuid4()}"
    async with new_worker(stream_client, CurrentProducerWorkflow) as worker:
        handle = await stream_client.start_workflow(
            CurrentProducerWorkflow.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        producer = await handle.result()
    run_id = handle.result_run_id
    assert run_id
    assert producer == Producer(
        id=f"wf:{run_id}", kind=ProducerKindWorkflow(WorkflowProducer(run_id=run_id))
    )
    # On a Store, the append writes as the Workflow, with its Workflow Task as the epoch.
    # The close passes the Store's check of that epoch too.
    stream = stream_client.get_stream(StreamRef(id=stream_id), body_type=str)
    [record] = await _records(stream)
    assert record.producer is not None
    assert record.producer.id == producer.id
    assert isinstance(record.producer.kind, ProducerKindWorkflow)
    assert record.producer.kind.value.epoch is not None
    assert record.producer.kind.value.epoch.attempt == 1
    described = await stream.describe()
    assert described.status == StreamStatus.StreamStatusClosed


@pytest.mark.parametrize("kind", ["workflow", *_ACTIVITY_INFOS, "client"])
async def test_producer_round_trips_through_the_default_data_converter(kind: str):
    producer = _producer_of_each_kind()[kind]
    converter = DataConverter.default
    [payload] = await converter.encode([producer])
    assert payload.metadata["messageType"] == b"temporal.api.stream.v1.Producer"
    # A Worker decodes an Activity's arguments and its result with their type hints.
    assert await converter.decode([payload], [Producer]) == [producer]
    # It decodes heartbeat details with no type hint, so they give the proto message.
    [detail] = await converter.decode([payload])
    assert isinstance(detail, temporalio.api.stream.v1.Producer)
    assert as_producer(detail) == producer


def test_as_producer_keeps_a_producer_and_refuses_other_values():
    producer = current_producer()
    assert as_producer(producer) is producer
    with pytest.raises(TypeError, match="not str"):
        as_producer("client:1")


@activity.defn
async def append_once_per_attempt(stream_id: str, keep_producer: bool) -> None:
    info = activity.info()
    stream = activity.client().get_stream(StreamRef(id=stream_id), body_type=str)
    if info.attempt == 1:
        await stream.append("first")
        activity.heartbeat(current_producer())
        raise RuntimeError("Failed the first attempt")
    producer = as_producer(info.heartbeat_details[0]) if keep_producer else None
    await stream.append("second", producer=producer)


@workflow.defn
class AppendOncePerAttemptWorkflow:
    @workflow.run
    async def run(self, stream_id: str, keep_producer: bool) -> None:
        await workflow.execute_activity(
            append_once_per_attempt,
            args=[stream_id, keep_producer],
            start_to_close_timeout=timedelta(seconds=10),
            heartbeat_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(milliseconds=10), maximum_attempts=2
            ),
        )


@pytest.mark.parametrize("keep_producer", [True, False])
async def test_activity_retry_appends_as_its_stored_producer(
    client: Client, keep_producer: bool
):
    stream_client = _with_store(client, MemoryStreamStore())
    stream = await stream_client.create_stream(
        id=f"stream-{uuid.uuid4()}", body_type=str
    )
    async with new_worker(
        stream_client,
        AppendOncePerAttemptWorkflow,
        activities=[append_once_per_attempt],
    ) as worker:
        # A FENCED append would fail the second and last attempt, and so the Workflow.
        await stream_client.execute_workflow(
            AppendOncePerAttemptWorkflow.run,
            args=[stream.ref.id, keep_producer],
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    first, second = await _records(stream)
    assert [first.body, second.body] == ["first", "second"]
    assert first.producer is not None and second.producer is not None
    assert first.producer.id == second.producer.id
    assert isinstance(first.producer.kind, ProducerKindWorkflowActivity)
    assert isinstance(second.producer.kind, ProducerKindWorkflowActivity)
    first_epoch = first.producer.kind.value.epoch
    second_epoch = second.producer.kind.value.epoch
    if keep_producer:
        # The retry continues the first attempt's epoch.
        assert second_epoch == first_epoch
    else:
        assert (first_epoch.attempt, second_epoch.attempt) == (1, 2)
