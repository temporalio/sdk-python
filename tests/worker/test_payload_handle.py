"""End-to-end tests for ValueHandle (Phase 1 prototype).

Demonstrates the headline behavior: a workflow whose run argument is annotated
``ValueHandle[T]`` receives a forward-only handle instead of an eagerly
downloaded value, forwards it to an activity without downloading, and the
activity acquires it on demand. The proof is the driver's retrieve count:
0 for pass-through (and on replay), exactly 1 when an activity materializes.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta

import temporalio.converter
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import ValueHandle
from temporalio.converter import ExternalStorage
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from tests.helpers import new_worker
from tests.test_extstore import InMemoryTestDriver

# Larger than the offload threshold so the run argument is externalized.
_BIG = "x" * 2000
_THRESHOLD = 1024


@activity.defn
async def consume_handle(data: ValueHandle[str]) -> int:
    # The activity needs the bytes, so it acquires them on demand via the handle's
    # own get_value method, which reaches the activity's converter at call time.
    value = await data.get_value()
    return len(value)


@activity.defn
async def ignore_handle(data: ValueHandle[str]) -> str:
    # Never materializes; the handle is just passed through.
    return "ignored"


@workflow.defn
class ForwardToConsumeWorkflow:
    @workflow.run
    async def run(self, data: ValueHandle[str]) -> int:
        # Forward the handle to an activity without materializing it here.
        return await workflow.execute_activity(
            consume_handle, data, start_to_close_timeout=timedelta(seconds=30)
        )


@workflow.defn
class ForwardToIgnoreWorkflow:
    @workflow.run
    async def run(self, data: ValueHandle[str]) -> str:
        return await workflow.execute_activity(
            ignore_handle, data, start_to_close_timeout=timedelta(seconds=30)
        )


@activity.defn
async def consume_prefixed(prefix: str, data: ValueHandle[str]) -> int:
    # A multi-arg activity: one plain value and one handle. Exercises the
    # positional multi-arg call path end to end.
    value = await data.get_value()
    return len(prefix) + len(value)


@workflow.defn
class ForwardMixedMultiArgWorkflow:
    @workflow.run
    async def run(self, data: ValueHandle[str]) -> int:
        # Forward the handle as the second positional arg of a two-arg activity,
        # mixed with a plain value, via the strongly-typed positional overloads.
        return await workflow.execute_activity(
            consume_prefixed,
            "pre-",
            data,
            start_to_close_timeout=timedelta(seconds=30),
        )


@activity.defn
async def produce_big() -> str:
    # An ordinary activity returning a large value; it is unchanged / unaware of
    # handles. The value is offloaded to external storage on completion.
    return _BIG


@workflow.defn
class ResultAsHandleConsumeWorkflow:
    @workflow.run
    async def run(self) -> int:
        # Upgrade an unchanged activity's result to a handle, then forward it to
        # an activity that materializes it.
        handle = await workflow.start_activity(
            produce_big, start_to_close_timeout=timedelta(seconds=30)
        ).as_value_handle()
        return await workflow.execute_activity(
            consume_handle, handle, start_to_close_timeout=timedelta(seconds=30)
        )


@workflow.defn
class ResultAsHandlePassThroughWorkflow:
    @workflow.run
    async def run(self) -> str:
        handle = await workflow.start_activity(
            produce_big, start_to_close_timeout=timedelta(seconds=30)
        ).as_value_handle()
        return await workflow.execute_activity(
            ignore_handle, handle, start_to_close_timeout=timedelta(seconds=30)
        )


@workflow.defn
class ChildProducerWorkflow:
    @workflow.run
    async def run(self) -> str:
        return _BIG


@workflow.defn
class ParentChildResultAsHandleWorkflow:
    @workflow.run
    async def run(self) -> str:
        # Upgrade an unchanged child workflow's result to a handle and forward it
        # without materializing it in this (parent) workflow.
        child = await workflow.start_child_workflow(ChildProducerWorkflow.run)
        handle = await child.as_value_handle()
        return await workflow.execute_activity(
            ignore_handle, handle, start_to_close_timeout=timedelta(seconds=30)
        )


@activity.defn
async def produce_handle_with_metadata() -> ValueHandle[str]:
    # The activity creates a handle from its (large) result and attaches metadata
    # that a consumer can probe without downloading the value.
    return await activity.create_value_handle(_BIG, metadata={"length": str(len(_BIG))})


@workflow.defn
class ProbeMetadataThenForwardWorkflow:
    @workflow.run
    async def run(self) -> int:
        handle = await workflow.execute_activity(
            produce_handle_with_metadata,
            start_to_close_timeout=timedelta(seconds=30),
        )
        # Probe metadata in the workflow to decide, without downloading the value.
        assert handle.metadata["length"] == str(len(_BIG))
        # Forward the handle to an activity that materializes it.
        return await workflow.execute_activity(
            consume_handle, handle, start_to_close_timeout=timedelta(seconds=30)
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


async def test_mixed_multi_arg_positional_forward(env: WorkflowEnvironment) -> None:
    driver = InMemoryTestDriver()
    client = await _client(env, driver)
    async with new_worker(
        client, ForwardMixedMultiArgWorkflow, activities=[consume_prefixed]
    ) as worker:
        result = await client.execute_workflow(
            ForwardMixedMultiArgWorkflow.run,
            args=[_BIG],
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    # Plain arg is inline; the forwarded handle is downloaded exactly once, when
    # the activity materializes it.
    assert result == len("pre-") + len(_BIG)
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


async def test_activity_result_as_handle_materializes_once(
    env: WorkflowEnvironment,
) -> None:
    driver = InMemoryTestDriver()
    client = await _client(env, driver)
    async with new_worker(
        client,
        ResultAsHandleConsumeWorkflow,
        activities=[produce_big, consume_handle],
    ) as worker:
        result = await client.execute_workflow(
            ResultAsHandleConsumeWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert result == len(_BIG)
    # The producing activity's result was offloaded, upgraded to a handle in the
    # workflow (not downloaded there), and materialized exactly once downstream.
    assert driver._retrieve_calls == 1


async def test_activity_result_as_handle_pass_through_no_download(
    env: WorkflowEnvironment,
) -> None:
    driver = InMemoryTestDriver()
    client = await _client(env, driver)
    async with new_worker(
        client,
        ResultAsHandlePassThroughWorkflow,
        activities=[produce_big, ignore_handle],
    ) as worker:
        result = await client.execute_workflow(
            ResultAsHandlePassThroughWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert result == "ignored"
    # The unchanged activity's offloaded result was never downloaded: the
    # workflow received it as a handle and forwarded it without materializing.
    assert driver._retrieve_calls == 0


async def test_child_workflow_result_as_handle_pass_through(
    env: WorkflowEnvironment,
) -> None:
    driver = InMemoryTestDriver()
    client = await _client(env, driver)
    async with new_worker(
        client,
        ParentChildResultAsHandleWorkflow,
        ChildProducerWorkflow,
        activities=[ignore_handle],
    ) as worker:
        result = await client.execute_workflow(
            ParentChildResultAsHandleWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert result == "ignored"
    # The child workflow's offloaded result was upgraded to a handle in the
    # parent and forwarded without ever being downloaded.
    assert driver._retrieve_calls == 0


async def test_activity_creates_handle_with_probeable_metadata(
    env: WorkflowEnvironment,
) -> None:
    driver = InMemoryTestDriver()
    client = await _client(env, driver)
    async with new_worker(
        client,
        ProbeMetadataThenForwardWorkflow,
        activities=[produce_handle_with_metadata, consume_handle],
    ) as worker:
        result = await client.execute_workflow(
            ProbeMetadataThenForwardWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
    assert result == len(_BIG)
    # Stored once (when the activity created the handle) and downloaded once (when
    # the consuming activity materialized it). The workflow probed metadata and
    # forwarded the handle without any download.
    assert driver._store_calls == 1
    assert driver._retrieve_calls == 1
