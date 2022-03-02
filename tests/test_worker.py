import asyncio
import concurrent.futures
import multiprocessing
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pytest

import temporalio.activity
import temporalio.client
import temporalio.exceptions
import temporalio.worker
from tests.helpers.worker import (
    KSAction,
    KSExecuteActivityAction,
    KSWorkflowParams,
    Worker,
)


async def test_activity_hello(client: temporalio.client.Client, worker: Worker):
    async def say_hello(name: str) -> str:
        return f"Hello, {name}!"

    result = await _execute_workflow_with_activity(
        client, worker, say_hello, "Temporal"
    )
    assert result.result == "Hello, Temporal!"


async def test_activity_info(client: temporalio.client.Client, worker: Worker):
    # Make sure info call outside of activity context fails
    assert not temporalio.activity.in_activity()
    with pytest.raises(RuntimeError) as err:
        temporalio.activity.info()
    assert str(err.value) == "Not in activity context"

    # Capture the info from the activity
    info: Optional[temporalio.activity.Info] = None

    async def capture_info() -> None:
        nonlocal info
        info = temporalio.activity.info()

    result = await _execute_workflow_with_activity(
        client, worker, capture_info, start_to_close_timeout_ms=4000
    )

    assert info
    assert info.activity_id
    assert info.activity_type == "capture_info"
    assert info.attempt == 1
    assert info.heartbeat_details == []
    assert info.heartbeat_timeout is None
    # TODO(cretz): Broken?
    # assert info.schedule_to_close_timeout is None
    assert abs(info.scheduled_time - datetime.now(timezone.utc)) < timedelta(seconds=5)
    assert info.start_to_close_timeout == timedelta(seconds=4)
    assert abs(info.started_time - datetime.now(timezone.utc)) < timedelta(seconds=5)
    assert info.task_queue == result.act_task_queue
    assert info.workflow_id == result.handle.id
    assert info.workflow_namespace == client.namespace
    assert info.workflow_run_id == result.handle.first_execution_run_id
    assert info.workflow_type == "kitchen_sink"


async def test_sync_activity_thread(client: temporalio.client.Client, worker: Worker):
    def some_activity() -> str:
        return f"activity name: {temporalio.activity.info().activity_type}"

    with concurrent.futures.ThreadPoolExecutor() as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            some_activity,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "activity name: some_activity"


def picklable_activity() -> str:
    return f"activity name: {temporalio.activity.info().activity_type}"


async def test_sync_activity_process(client: temporalio.client.Client, worker: Worker):
    with concurrent.futures.ProcessPoolExecutor() as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            picklable_activity,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "activity name: picklable_activity"


async def test_sync_activity_process_non_picklable(
    client: temporalio.client.Client, worker: Worker
):
    def some_activity() -> str:
        return f"activity name: {temporalio.activity.info().activity_type}"

    with pytest.raises(TypeError) as err:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            await _execute_workflow_with_activity(
                client,
                worker,
                some_activity,
                worker_config={"activity_executor": executor},
            )
    assert "must be picklable when using a process executor" in str(err.value)


async def test_activity_failure(client: temporalio.client.Client, worker: Worker):
    async def raise_error():
        raise RuntimeError("oh no!")

    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await _execute_workflow_with_activity(client, worker, raise_error)
    cause = err.value.cause
    assert isinstance(cause, temporalio.exceptions.ActivityError)
    cause = cause.__cause__
    assert isinstance(cause, temporalio.exceptions.ApplicationError)
    assert cause.message == "oh no!"
    assert cause.__cause__ is None


def picklable_activity_failure():
    raise RuntimeError("oh no!")


async def test_sync_activity_process_failure(
    client: temporalio.client.Client, worker: Worker
):
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            await _execute_workflow_with_activity(
                client,
                worker,
                picklable_activity_failure,
                worker_config={"activity_executor": executor},
            )
    cause = err.value.cause
    assert isinstance(cause, temporalio.exceptions.ActivityError)
    cause = cause.__cause__
    assert isinstance(cause, temporalio.exceptions.ApplicationError)
    assert cause.message == "oh no!"
    assert cause.__cause__ is None


async def test_activity_bad_params(client: temporalio.client.Client, worker: Worker):
    async def say_hello(name: str) -> str:
        return f"Hello, {name}!"

    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await _execute_workflow_with_activity(client, worker, say_hello)
    cause = err.value.cause
    assert isinstance(cause, temporalio.exceptions.ActivityError)
    cause = cause.__cause__
    assert isinstance(cause, temporalio.exceptions.ApplicationError)
    assert cause.message.endswith("missing 1 required positional argument: 'name'")
    assert cause.__cause__ is None


async def test_activity_kwonly_params(client: temporalio.client.Client, worker: Worker):
    async def say_hello(*, name: str) -> str:
        return f"Hello, {name}!"

    with pytest.raises(TypeError) as err:
        await _execute_workflow_with_activity(client, worker, say_hello, "blah")
    assert str(err.value).endswith("cannot have keyword-only arguments")


async def test_activity_cancel_catch(client: temporalio.client.Client, worker: Worker):
    async def wait_cancel() -> str:
        try:
            while True:
                await asyncio.sleep(0.1)
                temporalio.activity.heartbeat()
        except asyncio.CancelledError:
            return "Got cancelled error, cancelled? " + str(
                temporalio.activity.cancelled()
            )

    result = await _execute_workflow_with_activity(
        client,
        worker,
        wait_cancel,
        cancel_after_ms=100,
        wait_for_cancellation=True,
        heartbeat_timeout_ms=1000,
    )
    assert result.result == "Got cancelled error, cancelled? True"


async def test_activity_cancel_throw(client: temporalio.client.Client, worker: Worker):
    async def wait_cancel() -> str:
        while True:
            await asyncio.sleep(0.1)
            temporalio.activity.heartbeat()

    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await _execute_workflow_with_activity(
            client,
            worker,
            wait_cancel,
            cancel_after_ms=100,
            wait_for_cancellation=True,
            heartbeat_timeout_ms=1000,
        )
    # TODO(cretz): This is a side effect of Go where returning the activity
    # cancel looks like a workflow cancel. Change assertion if/when on another
    # lang.
    assert isinstance(err.value.cause, temporalio.exceptions.CancelledError)


async def test_sync_activity_thread_cancel(
    client: temporalio.client.Client, worker: Worker
):
    def wait_cancel() -> str:
        while not temporalio.activity.cancelled():
            time.sleep(0.1)
            temporalio.activity.heartbeat()
        return "Cancelled"

    with concurrent.futures.ThreadPoolExecutor() as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            wait_cancel,
            cancel_after_ms=100,
            wait_for_cancellation=True,
            heartbeat_timeout_ms=1000,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "Cancelled"


def picklable_activity_wait_cancel() -> str:
    while not temporalio.activity.cancelled():
        time.sleep(0.1)
        temporalio.activity.heartbeat()
    return "Cancelled"


async def test_sync_activity_process_cancel(
    client: temporalio.client.Client, worker: Worker
):
    with concurrent.futures.ProcessPoolExecutor() as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            picklable_activity_wait_cancel,
            cancel_after_ms=100,
            wait_for_cancellation=True,
            heartbeat_timeout_ms=1000,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "Cancelled"


# async def test_activity_catch_cancel():
#     raise NotImplementedError

# async def test_activity_not_exist():
#     raise NotImplementedError

# async def test_max_concurrent_activities():
#     raise NotImplementedError

# async def test_activity_type_hints():
#     raise NotImplementedError

# async def test_activity_async_with_executor():
#     raise NotImplementedError

# async def test_activity_interceptor():
#     raise NotImplementedError

# async def test_activity_async_completion():
#     raise NotImplementedError

# async def test_activity_heartbeat_details():
#     raise NotImplementedError

# async def test_activity_heartbeat_details_converter_fail():
#     raise NotImplementedError

# async def test_sync_activity_thread_heartbeat():
#     raise NotImplementedError

# async def test_sync_activity_process_heartbeat():
#     raise NotImplementedError

# async def test_sync_activity_process_heartbeat_non_picklable_details():
#     raise NotImplementedError

# async def test_activity_retry():
#     raise NotImplementedError

# async def test_activity_non_retry_error():
#     raise NotImplementedError

# async def test_activity_logging():
#     raise NotImplementedError

# async def test_activity_failure_with_details():
#     raise NotImplementedError

# async def test_activity_worker_shutdown():
#     raise NotImplementedError


@dataclass
class _ActivityResult:
    act_task_queue: str
    result: Any
    handle: temporalio.client.WorkflowHandle


async def _execute_workflow_with_activity(
    client: temporalio.client.Client,
    worker: Worker,
    fn: Callable,
    *args: Any,
    start_to_close_timeout_ms: Optional[int] = None,
    cancel_after_ms: Optional[int] = None,
    wait_for_cancellation: Optional[bool] = None,
    heartbeat_timeout_ms: Optional[int] = None,
    worker_config: temporalio.worker.WorkerConfig = {},
) -> _ActivityResult:
    act_task_queue = str(uuid.uuid4())
    async with temporalio.worker.Worker(
        client,
        task_queue=act_task_queue,
        activities={fn.__name__: fn},
        shared_state_manager=default_shared_state_manager(),
        **worker_config,
    ):
        handle = await client.start_workflow(
            "kitchen_sink",
            KSWorkflowParams(
                actions=[
                    KSAction(
                        execute_activity=KSExecuteActivityAction(
                            name=fn.__name__,
                            task_queue=act_task_queue,
                            args=args,
                            start_to_close_timeout_ms=start_to_close_timeout_ms,
                            cancel_after_ms=cancel_after_ms,
                            wait_for_cancellation=wait_for_cancellation,
                            heartbeat_timeout_ms=heartbeat_timeout_ms,
                        )
                    )
                ]
            ),
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        return _ActivityResult(
            act_task_queue=act_task_queue, result=await handle.result(), handle=handle
        )


_default_shared_state_manager: Optional[temporalio.worker.SharedStateManager] = None


def default_shared_state_manager() -> temporalio.worker.SharedStateManager:
    global _default_shared_state_manager
    if not _default_shared_state_manager:
        _default_shared_state_manager = (
            temporalio.worker.SharedStateManager.create_from_multiprocessing(
                multiprocessing.Manager()
            )
        )
    return _default_shared_state_manager
