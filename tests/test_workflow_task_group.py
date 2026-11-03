import asyncio
import sys
import uuid
from datetime import timedelta

import pytest

import temporalio.exceptions
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.worker import Replayer, Worker
from temporalio.workflow._asyncio import _OrderedTasks

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="TaskGroup requires Python 3.11"
)


@pytest.mark.parametrize("failure_in_body", [False, True])
async def test_task_group_cancellation_order(failure_in_body: bool):
    cancelled = []

    async def wait(index: int) -> None:
        try:
            await asyncio.Future()
        finally:
            cancelled.append(index)
            await asyncio.sleep(0)

    async def fail() -> None:
        raise ValueError("planned failure")

    tasks = []
    if sys.version_info >= (3, 11):
        with pytest.raises(ExceptionGroup) as exc:
            async with workflow.TaskGroup() as group:
                tasks = [group.create_task(wait(index)) for index in range(3)]
                await asyncio.sleep(0)
                if failure_in_body:
                    raise ValueError("planned failure")
                group.create_task(fail())
                await asyncio.Future()
        assert [str(error) for error in exc.value.exceptions] == ["planned failure"]
        assert all(task.done() for task in tasks)
        assert cancelled == [0, 1, 2]
        # An internal cancellation must not leak out after the exception is caught.
        await asyncio.sleep(0)


async def test_task_group_ordered_registry():
    tasks = [asyncio.create_task(asyncio.sleep(0)) for _ in range(3)]
    await asyncio.gather(*tasks)
    registry = _OrderedTasks()
    for task in tasks:
        registry.add(task)
    registry.add(tasks[1])
    assert list(registry) == tasks
    assert len(registry) == 3

    registry.discard(tasks[1])
    registry.discard(tasks[1])
    assert list(registry) == [tasks[0], tasks[2]]
    assert tasks[1] not in registry

    registry.add(tasks[1])
    assert list(registry) == [tasks[0], tasks[2], tasks[1]]
    assert tasks[1] in registry
    for task in tasks:
        registry.discard(task)
    assert not registry


async def test_task_group_external_cancellation():
    started = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def wait() -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned_up.set()

    async def run() -> None:
        async with workflow.TaskGroup() as group:
            group.create_task(wait())

    parent = asyncio.create_task(run())
    await started.wait()
    parent.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert cleaned_up.is_set()
    assert parent.cancelling() == 1


async def test_task_group_nested_failure():
    cancelled = []

    async def wait(name: str) -> None:
        try:
            await asyncio.Future()
        finally:
            cancelled.append(name)

    async def fail() -> None:
        await asyncio.sleep(0)
        raise ValueError("nested failure")

    async def inner() -> None:
        async with workflow.TaskGroup() as group:
            group.create_task(wait("inner"))
            group.create_task(fail())

    if sys.version_info >= (3, 11):
        with pytest.raises(ExceptionGroup) as exc:
            async with workflow.TaskGroup() as group:
                group.create_task(wait("outer"))
                group.create_task(inner())
        assert cancelled == ["inner", "outer"]
        nested = exc.value.exceptions[0]
        assert isinstance(nested, ExceptionGroup)
        assert isinstance(nested.exceptions[0], ValueError)


@pytest.mark.skipif(
    sys.version_info < (3, 13), reason="Cancellation preservation requires Python 3.13"
)
async def test_task_group_failure_preserves_external_cancellation():
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def wait() -> None:
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    async def fail() -> None:
        raise ValueError("failure")

    async def run() -> None:
        with pytest.raises(ExceptionGroup) as exc:
            async with workflow.TaskGroup() as group:
                group.create_task(wait())
                group.create_task(fail())
        assert [str(error) for error in exc.value.exceptions] == ["failure"]
        with pytest.raises(asyncio.CancelledError):
            await asyncio.sleep(0)

    parent = asyncio.create_task(run())
    await cleanup_started.wait()
    # Cancel while the group is already waiting for a failed child's siblings.
    parent.cancel()
    release_cleanup.set()
    await parent
    assert parent.cancelling() == 1


@workflow.defn
class TaskGroupWaitingChild:
    @workflow.run
    async def run(self) -> None:
        await asyncio.Future()


@activity.defn
async def task_group_waiting_activity() -> None:
    await asyncio.Future()


@workflow.defn
class TaskGroupMixedOperationsWorkflow:
    @workflow.run
    async def run(self) -> None:
        children_started = 0

        async def child(index: int) -> None:
            nonlocal children_started
            handle = await workflow.start_child_workflow(
                TaskGroupWaitingChild.run,
                id=f"{workflow.info().workflow_id}-child-{index}",
            )
            children_started += 1
            await handle

        async def fail() -> None:
            # Wait for child starts to be recorded before testing cancellation.
            await workflow.wait_condition(lambda: children_started == 2)
            raise ValueError("planned failure")

        try:
            async with workflow.TaskGroup() as group:
                for index in range(2):
                    group.create_task(workflow.sleep(1000))
                    group.create_task(
                        workflow.execute_activity(
                            task_group_waiting_activity,
                            start_to_close_timeout=timedelta(minutes=5),
                        )
                    )
                    group.create_task(child(index))
                group.create_task(fail())
        except ExceptionGroup as err:
            # Temporal wraps activity and child cancellations in failure exceptions.
            failures = [
                error
                for error in err.exceptions
                if not temporalio.exceptions.is_cancelled_exception(error)
            ]
            assert len(failures) == 1
            assert isinstance(failures[0], ValueError)
            assert str(failures[0]) == "planned failure"
        await workflow.sleep(0.01)


@pytest.mark.parametrize("max_cached_workflows", [0, 1000])
async def test_task_group_mixed_operations_replay(
    client: Client, max_cached_workflows: int
):
    async with Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[TaskGroupMixedOperationsWorkflow, TaskGroupWaitingChild],
        activities=[task_group_waiting_activity],
        max_cached_workflows=max_cached_workflows,
    ) as worker:
        handle = await client.start_workflow(
            TaskGroupMixedOperationsWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        await handle.result()
        history = await handle.fetch_history()

    cancel_types = {
        EventType.EVENT_TYPE_TIMER_CANCELED,
        EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED,
        EventType.EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED,
    }
    assert [
        event.event_type for event in history.events if event.event_type in cancel_types
    ] == [
        EventType.EVENT_TYPE_TIMER_CANCELED,
        EventType.EVENT_TYPE_ACTIVITY_TASK_CANCEL_REQUESTED,
        EventType.EVENT_TYPE_REQUEST_CANCEL_EXTERNAL_WORKFLOW_EXECUTION_INITIATED,
    ] * 2
    for _ in range(3):
        await Replayer(
            workflows=[TaskGroupMixedOperationsWorkflow, TaskGroupWaitingChild]
        ).replay_workflow(history)
