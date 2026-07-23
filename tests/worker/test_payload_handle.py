"""End-to-end tests for PayloadHandle (Phase 1 prototype).

Demonstrates the headline behavior: a workflow whose run argument is annotated
``PayloadHandle[T]`` receives a forward-only handle instead of an eagerly
downloaded value, forwards it to an activity without downloading, and the
activity materializes it on demand. The proof is the driver's retrieve count:
0 for pass-through (and on replay), exactly 1 when an activity materializes.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta

import temporalio.converter
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.converter import ExternalStorage, PayloadHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from tests.helpers import new_worker
from tests.test_extstore import InMemoryTestDriver

# Larger than the offload threshold so the run argument is externalized.
_BIG = "x" * 2000
_THRESHOLD = 1024


@activity.defn
async def consume_handle(data: PayloadHandle[str]) -> int:
    # The activity needs the bytes, so it materializes on demand.
    value = await data.materialize()
    return len(value)


@activity.defn
async def ignore_handle(data: PayloadHandle[str]) -> str:
    # Never materializes; the handle is just passed through.
    return "ignored"


@workflow.defn
class ForwardToConsumeWorkflow:
    @workflow.run
    async def run(self, data: PayloadHandle[str]) -> int:
        # Forward the handle to an activity without materializing it here.
        return await workflow.execute_activity(
            consume_handle, data, start_to_close_timeout=timedelta(seconds=30)
        )


@workflow.defn
class ForwardToIgnoreWorkflow:
    @workflow.run
    async def run(self, data: PayloadHandle[str]) -> str:
        return await workflow.execute_activity(
            ignore_handle, data, start_to_close_timeout=timedelta(seconds=30)
        )


def _data_converter(driver: InMemoryTestDriver) -> temporalio.converter.DataConverter:
    return dataclasses.replace(
        temporalio.converter.default(),
        external_storage=ExternalStorage(
            drivers=[driver], payload_size_threshold=_THRESHOLD
        ),
    )


async def _client(env: WorkflowEnvironment, driver: InMemoryTestDriver) -> Client:
    return await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=_data_converter(driver),
    )


async def test_activity_materializes_handle_once(env: WorkflowEnvironment) -> None:
    driver = InMemoryTestDriver()
    client = await _client(env, driver)
    async with new_worker(
        client, ForwardToConsumeWorkflow, activities=[consume_handle]
    ) as worker:
        # The caller sends the real value; the workflow opts to receive it as a
        # handle, so pass via the loosely-typed args form.
        result = await client.execute_workflow(
            ForwardToConsumeWorkflow.run,
            args=[_BIG],
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert result == len(_BIG)
    # Downloaded exactly once, at the point the activity materialized it.
    assert driver._retrieve_calls == 1


async def test_workflow_pass_through_no_download(env: WorkflowEnvironment) -> None:
    driver = InMemoryTestDriver()
    client = await _client(env, driver)
    async with new_worker(
        client, ForwardToIgnoreWorkflow, activities=[ignore_handle]
    ) as worker:
        handle = await client.start_workflow(
            ForwardToIgnoreWorkflow.run,
            args=[_BIG],
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        assert await handle.result() == "ignored"

    # The workflow forwarded the handle without ever downloading it.
    assert driver._retrieve_calls == 0

    # Replaying the same history must also avoid any download.
    history = await handle.fetch_history()
    replay_result = await Replayer(
        workflows=[ForwardToIgnoreWorkflow],
        data_converter=_data_converter(driver),
    ).replay_workflow(history, raise_on_replay_failure=True)
    assert replay_result is not None
    assert driver._retrieve_calls == 0
