import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest

from temporalio import activity, workflow
from temporalio.client import ActivityFailedError, Client
from temporalio.common import ActivityExecutionStatus
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.worker import Worker


@activity.defn
async def increment(input: int) -> int:
    return input + 1


async def test_describe(client: Client):
    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )
    desc = await activity_handle.describe()
    assert desc.activity_id == activity_id
    assert desc.run_id == activity_handle.run_id
    assert desc.activity_type == "increment"
    assert desc.task_queue == task_queue
    assert desc.status == ActivityExecutionStatus.RUNNING


async def test_get_result(client: Client):
    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )
    result_via_execute_activity = client.execute_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[increment],
    ):
        assert await activity_handle.result() == 2
        assert await result_via_execute_activity == 2


@dataclass
class ActivityInput:
    wait_for_signal_workflow_id: str
    wait_for_activity_start_workflow_id: str | None = None


@activity.defn
async def async_activity(input: ActivityInput) -> int:
    # Notify test that the activity has started and is ready to be completed manually
    await (
        activity.client()
        .get_workflow_handle(input.wait_for_signal_workflow_id)
        .signal(WaitForSignalWorkflow.signal)
    )
    activity.raise_complete_async()


async def test_manual_completion(client: Client):
    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    wait_for_signal_workflow_id = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        async_activity,
        args=(
            ActivityInput(wait_for_signal_workflow_id=wait_for_signal_workflow_id),
        ),  # TODO: overloads
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[async_activity],
        workflows=[WaitForSignalWorkflow],
    ):
        # Wait for activity to start
        await client.execute_workflow(
            WaitForSignalWorkflow.run,
            id=wait_for_signal_workflow_id,
            task_queue=task_queue,
        )
        # Complete activity manually
        async_activity_handle = client.get_async_activity_handle(
            activity_id=activity_id,
            run_id=activity_handle.run_id,
        )
        await async_activity_handle.complete(7)
        assert await activity_handle.result() == 7

        desc = await activity_handle.describe()
        assert desc.status == ActivityExecutionStatus.COMPLETED


async def test_manual_cancellation(client: Client):
    pytest.skip(reason="https://temporalio.atlassian.net/browse/ACT-647")
    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    wait_for_signal_workflow_id = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        async_activity,
        args=(
            ActivityInput(wait_for_signal_workflow_id=wait_for_signal_workflow_id),
        ),  # TODO: overloads
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )

    async with Worker(
        client,
        task_queue=task_queue,
        activities=[async_activity],
        workflows=[WaitForSignalWorkflow],
    ):
        await client.execute_workflow(
            WaitForSignalWorkflow.run,
            id=wait_for_signal_workflow_id,
            task_queue=task_queue,
        )
        async_activity_handle = client.get_async_activity_handle(
            activity_id=activity_id,
            run_id=activity_handle.run_id,
        )
        await async_activity_handle.report_cancellation("Test cancellation")
        with pytest.raises(ActivityFailedError) as err:
            await activity_handle.result()
        assert isinstance(err.value.cause, CancelledError)
        assert list(err.value.cause.details) == ["Test cancellation"]

        desc = await activity_handle.describe()
        assert desc.status == ActivityExecutionStatus.CANCELED


async def test_manual_failure(client: Client):
    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    wait_for_signal_workflow_id = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        async_activity,
        args=(
            ActivityInput(wait_for_signal_workflow_id=wait_for_signal_workflow_id),
        ),  # TODO: overloads
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )
    async with Worker(
        client,
        task_queue=task_queue,
        activities=[async_activity],
        workflows=[WaitForSignalWorkflow],
    ):
        await client.execute_workflow(
            WaitForSignalWorkflow.run,
            id=wait_for_signal_workflow_id,
            task_queue=task_queue,
        )
        async_activity_handle = client.get_async_activity_handle(
            activity_id=activity_id,
            run_id=activity_handle.run_id,
        )
        await async_activity_handle.fail(
            ApplicationError("Test failure", non_retryable=True)
        )
        with pytest.raises(ActivityFailedError) as err:
            await activity_handle.result()
        assert isinstance(err.value.cause, ApplicationError)
        assert str(err.value.cause) == "Test failure"

        desc = await activity_handle.describe()
        assert desc.status == ActivityExecutionStatus.FAILED


@activity.defn
async def activity_for_testing_heartbeat(input: ActivityInput) -> str:
    info = activity.info()
    if info.attempt == 1:
        # Signal that activity has started (only on first attempt)
        if input.wait_for_activity_start_workflow_id:
            await (
                activity.client()
                .get_workflow_handle(
                    workflow_id=input.wait_for_activity_start_workflow_id,
                )
                .signal(WaitForSignalWorkflow.signal)
            )
        wait_for_heartbeat_wf_handle = await activity.client().start_workflow(
            WaitForSignalWorkflow.run,
            id=input.wait_for_signal_workflow_id,
            task_queue=activity.info().task_queue,
        )
        # Wait for test to notify that it has sent heartbeat
        await wait_for_heartbeat_wf_handle.result()
        raise Exception("Intentional error to force retry")
    elif info.attempt == 2:
        [heartbeat_data] = info.heartbeat_details
        assert isinstance(heartbeat_data, str)
        return heartbeat_data
    else:
        raise AssertionError(f"Unexpected attempt number: {info.attempt}")


async def test_manual_heartbeat(client: Client):
    activity_id = str(uuid.uuid4())
    task_queue = str(uuid.uuid4())
    wait_for_signal_workflow_id = str(uuid.uuid4())
    wait_for_activity_start_workflow_id = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        activity_for_testing_heartbeat,
        args=(
            ActivityInput(
                wait_for_signal_workflow_id=wait_for_signal_workflow_id,
                wait_for_activity_start_workflow_id=wait_for_activity_start_workflow_id,
            ),
        ),  # TODO: overloads
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )
    wait_for_activity_start_wf_handle = await client.start_workflow(
        WaitForSignalWorkflow.run,
        id=wait_for_activity_start_workflow_id,
        task_queue=task_queue,
    )
    async with Worker(
        client,
        task_queue=task_queue,
        activities=[activity_for_testing_heartbeat],
        workflows=[WaitForSignalWorkflow],
    ):
        async_activity_handle = client.get_async_activity_handle(
            activity_id=activity_id,
            run_id=activity_handle.run_id,
        )
        await wait_for_activity_start_wf_handle.result()
        await async_activity_handle.heartbeat("Test heartbeat details")
        await client.get_workflow_handle(
            workflow_id=wait_for_signal_workflow_id,
        ).signal(WaitForSignalWorkflow.signal)
        assert await activity_handle.result() == "Test heartbeat details"


# Utilities


@workflow.defn
class WaitForSignalWorkflow:
    # Like a global asyncio.Event()

    def __init__(self) -> None:
        self.signal_received = asyncio.Event()

    @workflow.run
    async def run(self) -> None:
        await self.signal_received.wait()

    @workflow.signal
    def signal(self) -> None:
        self.signal_received.set()
