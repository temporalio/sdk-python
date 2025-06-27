import asyncio
import concurrent.futures
import logging
import logging.handlers
import multiprocessing
import os
import queue
import signal
import threading
import time
import uuid
from concurrent.futures.process import BrokenProcessPool
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, NoReturn, Optional, Sequence, Type

import temporalio.api.workflowservice.v1
from temporalio import activity, workflow
from temporalio.api.workflowservice.v1.request_response_pb2 import ResetActivityRequest
from temporalio.client import (
    AsyncActivityHandle,
    Client,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.common import RawValue, RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    TimeoutError,
    TimeoutType,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
    SharedStateManager,
    Worker,
    WorkerConfig,
)
from tests.helpers.worker import (
    ExternalWorker,
    KSAction,
    KSExecuteActivityAction,
    KSWorkflowParams,
)

# Passing through because Python 3.9 has an import bug at
# https://github.com/python/cpython/issues/91351
with workflow.unsafe.imports_passed_through():
    import pytest


_default_shared_state_manager = SharedStateManager.create_from_multiprocessing(
    multiprocessing.Manager()
)

default_max_concurrent_activities = 50


async def test_activity_hello(client: Client, worker: ExternalWorker):
    @activity.defn
    async def say_hello(name: str) -> str:
        return f"Hello, {name}!"

    result = await _execute_workflow_with_activity(
        client, worker, say_hello, "Temporal"
    )
    assert result.result == "Hello, Temporal!"


async def test_activity_without_decorator(client: Client, worker: ExternalWorker):
    async def say_hello(name: str) -> str:
        return f"Hello, {name}!"

    with pytest.raises(TypeError) as err:
        await _execute_workflow_with_activity(client, worker, say_hello, "Temporal")
    assert "Activity say_hello missing attributes" in str(err.value)


async def test_activity_custom_name(client: Client, worker: ExternalWorker):
    @activity.defn(name="my custom activity name!")
    async def get_name(name: str) -> str:
        return f"Name: {activity.info().activity_type}"

    result = await _execute_workflow_with_activity(
        client,
        worker,
        get_name,
        "Temporal",
        activity_name_override="my custom activity name!",
    )
    assert result.result == "Name: my custom activity name!"


async def test_activity_info(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1426"
        )
    # Make sure info call outside of activity context fails
    assert not activity.in_activity()
    with pytest.raises(RuntimeError) as err:
        activity.info()
    assert str(err.value) == "Not in activity context"

    # Capture the info from the activity
    info: Optional[activity.Info] = None

    @activity.defn
    async def capture_info() -> None:
        nonlocal info
        info = activity.info()

    result = await _execute_workflow_with_activity(
        client, worker, capture_info, start_to_close_timeout_ms=4000
    )

    assert info
    assert info.activity_id
    assert info.activity_type == "capture_info"
    assert info.attempt == 1
    assert abs(
        info.current_attempt_scheduled_time - datetime.now(timezone.utc)
    ) < timedelta(seconds=5)
    assert info.heartbeat_details == []
    assert info.heartbeat_timeout is None
    assert not info.is_local
    assert info.schedule_to_close_timeout is None
    assert abs(info.scheduled_time - datetime.now(timezone.utc)) < timedelta(seconds=5)
    assert info.start_to_close_timeout == timedelta(seconds=4)
    assert abs(info.started_time - datetime.now(timezone.utc)) < timedelta(seconds=5)
    assert info.task_queue == result.act_task_queue
    assert info.task_token
    assert info.workflow_id == result.handle.id
    assert info.workflow_namespace == client.namespace
    assert info.workflow_run_id == result.handle.first_execution_run_id
    assert info.workflow_type == "kitchen_sink"


async def test_sync_activity_thread(client: Client, worker: ExternalWorker):
    @activity.defn
    def some_activity() -> str:
        return f"activity name: {activity.info().activity_type}"

    # We intentionally leave max_workers by default in the thread pool executor
    # to confirm that the warning is triggered
    with concurrent.futures.ThreadPoolExecutor() as executor:
        with pytest.warns(
            UserWarning,
            match=f"Worker max_concurrent_activities is {default_max_concurrent_activities} but activity_executor's max_workers is only",
        ):
            result = await _execute_workflow_with_activity(
                client,
                worker,
                some_activity,
                worker_config={"activity_executor": executor},
            )
    assert result.result == "activity name: some_activity"


@activity.defn
def picklable_activity() -> str:
    return f"activity name: {activity.info().activity_type}"


async def test_sync_activity_process(client: Client, worker: ExternalWorker):
    # We intentionally leave max_workers by default in the process pool executor
    # to confirm that the warning is triggered
    with concurrent.futures.ProcessPoolExecutor() as executor:
        with pytest.warns(
            UserWarning,
            match=f"Worker max_concurrent_activities is {default_max_concurrent_activities} but activity_executor's max_workers is only",
        ):
            result = await _execute_workflow_with_activity(
                client,
                worker,
                picklable_activity,
                worker_config={"activity_executor": executor},
            )
    assert result.result == "activity name: picklable_activity"


async def test_sync_activity_process_non_picklable(
    client: Client, worker: ExternalWorker
):
    @activity.defn
    def some_activity() -> str:
        return f"activity name: {activity.info().activity_type}"

    with pytest.raises(TypeError) as err:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            await _execute_workflow_with_activity(
                client,
                worker,
                some_activity,
                worker_config={"activity_executor": executor},
            )
    assert "must be picklable when using a process executor" in str(err.value)


async def test_activity_failure(client: Client, worker: ExternalWorker):
    @activity.defn
    async def raise_error():
        raise RuntimeError("oh no!")

    with pytest.raises(WorkflowFailureError) as err:
        await _execute_workflow_with_activity(client, worker, raise_error)
    assert str(assert_activity_application_error(err.value)) == "RuntimeError: oh no!"


@activity.defn
def picklable_activity_failure():
    raise RuntimeError("oh no!")


async def test_sync_activity_process_failure(client: Client, worker: ExternalWorker):
    with pytest.raises(WorkflowFailureError) as err:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            await _execute_workflow_with_activity(
                client,
                worker,
                picklable_activity_failure,
                worker_config={"activity_executor": executor},
            )
    assert str(assert_activity_application_error(err.value)) == "RuntimeError: oh no!"


async def test_activity_bad_params(client: Client, worker: ExternalWorker):
    @activity.defn
    async def say_hello(name: str) -> str:
        return f"Hello, {name}!"

    with pytest.raises(WorkflowFailureError) as err:
        await _execute_workflow_with_activity(client, worker, say_hello)
    assert str(assert_activity_application_error(err.value)).endswith(
        "missing 1 required positional argument: 'name'"
    )


async def test_activity_kwonly_params():
    with pytest.raises(TypeError) as err:

        @activity.defn
        async def say_hello(*, name: str) -> str:
            return f"Hello, {name}!"

    assert str(err.value).endswith("cannot have keyword-only arguments")


async def test_activity_cancel_catch(client: Client, worker: ExternalWorker):
    @activity.defn
    async def wait_cancel() -> str:
        try:
            while True:
                await asyncio.sleep(0.3)
                activity.heartbeat()
        except asyncio.CancelledError:
            return "Got cancelled error, cancelled? " + str(activity.is_cancelled())

    result = await _execute_workflow_with_activity(
        client,
        worker,
        wait_cancel,
        cancel_after_ms=100,
        wait_for_cancellation=True,
        heartbeat_timeout_ms=2000,
    )
    assert result.result == "Got cancelled error, cancelled? True"


async def test_activity_cancel_throw(client: Client, worker: ExternalWorker):
    @activity.defn
    async def wait_cancel() -> str:
        while True:
            await asyncio.sleep(0.3)
            activity.heartbeat()

    with pytest.raises(WorkflowFailureError) as err:
        await _execute_workflow_with_activity(
            client,
            worker,
            wait_cancel,
            cancel_after_ms=100,
            wait_for_cancellation=True,
            heartbeat_timeout_ms=1000,
        )
    assert isinstance(err.value.cause, ActivityError)
    assert isinstance(err.value.cause.cause, CancelledError)


async def test_sync_activity_thread_cancel_caught(
    client: Client, worker: ExternalWorker
):
    @activity.defn
    def wait_cancel() -> str:
        try:
            while True:
                time.sleep(1)
                activity.heartbeat()
        except CancelledError:
            assert activity.is_cancelled()
            return "Cancelled"

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=default_max_concurrent_activities
    ) as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            wait_cancel,
            cancel_after_ms=100,
            wait_for_cancellation=True,
            heartbeat_timeout_ms=3000,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "Cancelled"


async def test_sync_activity_thread_cancel_uncaught(
    client: Client, worker: ExternalWorker
):
    @activity.defn
    def wait_cancel() -> NoReturn:
        while True:
            time.sleep(1)
            activity.heartbeat()

    with pytest.raises(WorkflowFailureError) as err:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=default_max_concurrent_activities
        ) as executor:
            await _execute_workflow_with_activity(
                client,
                worker,
                wait_cancel,
                cancel_after_ms=100,
                wait_for_cancellation=True,
                heartbeat_timeout_ms=3000,
                worker_config={"activity_executor": executor},
            )
    assert isinstance(err.value.cause, ActivityError)
    assert isinstance(err.value.cause.cause, CancelledError)


async def test_sync_activity_thread_cancel_exception_disabled(
    client: Client, worker: ExternalWorker
):
    @activity.defn(no_thread_cancel_exception=True)
    def wait_cancel() -> str:
        while True:
            time.sleep(1)
            activity.heartbeat()
            if activity.is_cancelled():
                # Heartbeat again just to confirm nothing happens
                time.sleep(1)
                activity.heartbeat()
                return "Cancelled"

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=default_max_concurrent_activities
    ) as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            wait_cancel,
            cancel_after_ms=100,
            wait_for_cancellation=True,
            heartbeat_timeout_ms=3000,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "Cancelled"


async def test_sync_activity_thread_cancel_exception_shielded(
    client: Client, worker: ExternalWorker
):
    events: List[str] = []

    @activity.defn
    def wait_cancel() -> None:
        events.append("pre1")
        with activity.shield_thread_cancel_exception():
            events.append("pre2")
            with activity.shield_thread_cancel_exception():
                events.append("pre3")
                while not activity.is_cancelled():
                    time.sleep(1)
                    activity.heartbeat()
                events.append("post3")
            events.append("post2")
        events.append("post1")

    with pytest.raises(WorkflowFailureError) as err:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=default_max_concurrent_activities
        ) as executor:
            await _execute_workflow_with_activity(
                client,
                worker,
                wait_cancel,
                cancel_after_ms=100,
                wait_for_cancellation=True,
                heartbeat_timeout_ms=3000,
                worker_config={"activity_executor": executor},
            )
    assert isinstance(err.value.cause, ActivityError)
    assert isinstance(err.value.cause.cause, CancelledError)
    # This will have every event except post1 because that's where it throws
    assert events == ["pre1", "pre2", "pre3", "post3", "post2"]


sync_activity_waiting_cancel = threading.Event()


@activity.defn
def sync_activity_wait_cancel():
    sync_activity_waiting_cancel.set()
    while True:
        time.sleep(1)
        activity.heartbeat()


# We don't sandbox because Python logging uses multiprocessing if it's present
# which we don't want to get warnings about
@workflow.defn(sandboxed=False)
class CancelOnWorkerShutdownWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_activity(
            sync_activity_wait_cancel,
            start_to_close_timeout=timedelta(hours=1),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


# This test used to fail because we were sending a cancelled error and the
# server doesn't allow that
async def test_sync_activity_thread_cancel_on_worker_shutdown(client: Client):
    task_queue = f"tq-{uuid.uuid4()}"

    def new_worker() -> Worker:
        return Worker(
            client,
            task_queue=task_queue,
            activities=[sync_activity_wait_cancel],
            workflows=[CancelOnWorkerShutdownWorkflow],
            activity_executor=executor,
            max_concurrent_activities=default_max_concurrent_activities,
            max_cached_workflows=0,
        )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=default_max_concurrent_activities
    ) as executor:
        async with new_worker():
            # Start the workflow
            handle = await client.start_workflow(
                CancelOnWorkerShutdownWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=task_queue,
            )
            # Wait for activity to start
            assert await asyncio.get_running_loop().run_in_executor(
                executor, lambda: sync_activity_waiting_cancel.wait(20)
            )
            # Shut down the worker
    # Start the worker again and wait for result
    with pytest.raises(WorkflowFailureError) as err:
        async with new_worker():
            await handle.result()
    assert isinstance(err.value.cause, ActivityError)
    assert isinstance(err.value.cause.cause, ApplicationError)
    assert "activity did not complete in time" in err.value.cause.cause.message


@activity.defn
def picklable_activity_wait_cancel() -> str:
    while not activity.is_cancelled():
        time.sleep(1)
        activity.heartbeat()
    return "Cancelled"


async def test_sync_activity_process_cancel(client: Client, worker: ExternalWorker):
    with concurrent.futures.ProcessPoolExecutor() as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            picklable_activity_wait_cancel,
            cancel_after_ms=100,
            wait_for_cancellation=True,
            heartbeat_timeout_ms=3000,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "Cancelled"


@activity.defn
def picklable_activity_raise_cancel() -> str:
    while not activity.is_cancelled():
        time.sleep(1)
        activity.heartbeat()
    raise CancelledError("Cancelled")


async def test_sync_activity_process_cancel_uncaught(
    client: Client, worker: ExternalWorker
):
    with pytest.raises(WorkflowFailureError) as err:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            await _execute_workflow_with_activity(
                client,
                worker,
                picklable_activity_raise_cancel,
                cancel_after_ms=100,
                wait_for_cancellation=True,
                heartbeat_timeout_ms=5000,
                worker_config={"activity_executor": executor},
            )
    assert isinstance(err.value.cause, ActivityError)
    assert isinstance(err.value.cause.cause, CancelledError)


async def test_activity_does_not_exist(client: Client, worker: ExternalWorker):
    @activity.defn
    async def say_hello(name: str) -> str:
        return f"Hello, {name}!"

    with pytest.raises(WorkflowFailureError) as err:
        act_task_queue = str(uuid.uuid4())
        async with Worker(client, task_queue=act_task_queue, activities=[say_hello]):
            await client.execute_workflow(
                "kitchen_sink",
                KSWorkflowParams(
                    actions=[
                        KSAction(
                            execute_activity=KSExecuteActivityAction(
                                name="wrong_activity", task_queue=act_task_queue
                            )
                        )
                    ]
                ),
                id=str(uuid.uuid4()),
                task_queue=worker.task_queue,
            )
    assert "is not registered" in str(assert_activity_application_error(err.value))


async def test_max_concurrent_activities(client: Client, worker: ExternalWorker):
    seen_indexes: List[int] = []
    complete_activities_event = asyncio.Event()

    @activity.defn
    async def some_activity(index: int) -> str:
        seen_indexes.append(index)
        # Wait here to hold up the activity
        await complete_activities_event.wait()
        return ""

    # Only allow 42 activities, but try to execute 43. Make a short schedule to
    # start timeout but a long schedule to close timeout.
    with pytest.raises(WorkflowFailureError) as err:
        await _execute_workflow_with_activity(
            client,
            worker,
            some_activity,
            count=43,
            index_as_arg=True,
            schedule_to_close_timeout_ms=5000,
            schedule_to_start_timeout_ms=1000,
            worker_config={"max_concurrent_activities": 42},
            on_complete=complete_activities_event.set,
        )
    timeout = assert_activity_error(err.value)
    assert isinstance(timeout, TimeoutError)
    assert timeout.type == TimeoutType.SCHEDULE_TO_START


@dataclass
class SomeClass1:
    foo: int


@dataclass
class SomeClass2:
    foo: str
    bar: Optional[SomeClass1] = None


async def test_activity_type_hints(client: Client, worker: ExternalWorker):
    activity_param1: SomeClass2

    @activity.defn
    async def some_activity(param1: SomeClass2, param2: str) -> str:
        nonlocal activity_param1
        activity_param1 = param1
        return f"param1: {type(param1)}, param2: {type(param2)}"

    result = await _execute_workflow_with_activity(
        client,
        worker,
        some_activity,
        SomeClass2(foo="str1", bar=SomeClass1(foo=123)),
        "123",
    )
    assert (
        result.result
        == "param1: <class 'tests.worker.test_activity.SomeClass2'>, param2: <class 'str'>"
    )
    assert activity_param1 == SomeClass2(foo="str1", bar=SomeClass1(foo=123))


async def test_activity_heartbeat_details(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("https://github.com/temporalio/sdk-java/issues/2459")

    @activity.defn
    async def some_activity() -> str:
        info = activity.info()
        count = int(next(iter(info.heartbeat_details))) if info.heartbeat_details else 0
        activity.logger.debug("Changing count from %s to %s", count, count + 9)
        count += 9
        activity.heartbeat(count)
        if count < 30:
            raise RuntimeError("Try again!")
        return f"final count: {count}"

    result = await _execute_workflow_with_activity(
        client,
        worker,
        some_activity,
        retry_max_attempts=4,
    )
    assert result.result == "final count: 36"


class NotSerializableValue:
    pass


async def test_activity_heartbeat_details_converter_fail(
    client: Client, worker: ExternalWorker
):
    @activity.defn
    async def some_activity() -> str:
        activity.heartbeat(NotSerializableValue())
        # Since the above fails, it will cause this task to be cancelled on the
        # next event loop iteration, so we sleep for a short time to allow that
        # iteration to occur
        await asyncio.sleep(0.05)
        return "Should not get here"

    with pytest.raises(WorkflowFailureError) as err:
        await _execute_workflow_with_activity(client, worker, some_activity)
    assert str(assert_activity_application_error(err.value)).endswith(
        "is not JSON serializable"
    )


async def test_activity_heartbeat_details_timeout(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    # TODO(cretz): Fix
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1427"
        )

    @activity.defn
    async def some_activity() -> str:
        activity.heartbeat("some details!")
        await asyncio.sleep(3)
        return "Should not get here"

    # Have a 1s heartbeat timeout that we won't meet with a second heartbeat
    # then check the timeout's details
    with pytest.raises(WorkflowFailureError) as err:
        await _execute_workflow_with_activity(
            client, worker, some_activity, heartbeat_timeout_ms=1000
        )
    timeout = assert_activity_error(err.value)
    assert isinstance(timeout, TimeoutError)
    assert str(timeout) == "activity Heartbeat timeout"
    assert timeout.type == TimeoutType.HEARTBEAT
    assert list(timeout.last_heartbeat_details) == ["some details!"]


@activity.defn
def picklable_heartbeat_details_activity() -> str:
    info = activity.info()
    some_list: List[str] = (
        next(iter(info.heartbeat_details)) if info.heartbeat_details else []
    )
    some_list.append(f"attempt: {info.attempt}")
    activity.logger.debug("Heartbeating with value: %s", some_list)
    activity.heartbeat(some_list)
    if len(some_list) < 2:
        raise RuntimeError(f"Try again, list contains: {some_list}")
    return ", ".join(some_list)


async def test_sync_activity_thread_heartbeat_details(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("https://github.com/temporalio/sdk-java/issues/2459")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=default_max_concurrent_activities
    ) as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            picklable_heartbeat_details_activity,
            retry_max_attempts=2,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "attempt: 1, attempt: 2"


async def test_sync_activity_process_heartbeat_details(
    client: Client, worker: ExternalWorker, env: WorkflowEnvironment
):
    if env.supports_time_skipping:
        pytest.skip("https://github.com/temporalio/sdk-java/issues/2459")

    with concurrent.futures.ProcessPoolExecutor() as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            picklable_heartbeat_details_activity,
            retry_max_attempts=2,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "attempt: 1, attempt: 2"


@activity.defn
def picklable_activity_non_pickable_heartbeat_details() -> str:
    activity.heartbeat(lambda: "cannot pickle lambda by default")
    return "Should not get here"


async def test_sync_activity_process_non_picklable_heartbeat_details(
    client: Client, worker: ExternalWorker
):
    with pytest.raises(WorkflowFailureError) as err:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            await _execute_workflow_with_activity(
                client,
                worker,
                picklable_activity_non_pickable_heartbeat_details,
                worker_config={"activity_executor": executor},
            )
    msg = str(assert_activity_application_error(err.value))
    # TODO: different messages can apparently be produced across runs/platforms
    # See e.g. https://github.com/temporalio/sdk-python/actions/runs/10455232879/job/28949714969?pr=571
    assert (
        "Can't pickle" in msg
        or "Can't get local object 'picklable_activity_non_pickable_heartbeat_details.<locals>.<lambda>'"
        in msg
    )


async def test_activity_error_non_retryable(client: Client, worker: ExternalWorker):
    @activity.defn
    async def some_activity():
        if activity.info().attempt < 2:
            raise ApplicationError("Retry me", non_retryable=False)
        # We'll test error details while we're here
        raise ApplicationError("Do not retry me", "detail1", 123, non_retryable=True)

    with pytest.raises(WorkflowFailureError) as err:
        await _execute_workflow_with_activity(
            client,
            worker,
            some_activity,
            retry_max_attempts=100,
        )
    app_err = assert_activity_application_error(err.value)
    assert str(app_err) == "Do not retry me"
    assert list(app_err.details) == ["detail1", 123]


async def test_activity_error_non_retryable_type(
    client: Client, worker: ExternalWorker
):
    @activity.defn
    async def some_activity():
        if activity.info().attempt < 2:
            raise ApplicationError("Retry me", type="Can retry me")
        raise ApplicationError("Do not retry me", type="Cannot retry me")

    with pytest.raises(WorkflowFailureError) as err:
        await _execute_workflow_with_activity(
            client,
            worker,
            some_activity,
            retry_max_attempts=100,
            non_retryable_error_types=["Cannot retry me"],
        )
    assert (
        str(assert_activity_application_error(err.value))
        == "Cannot retry me: Do not retry me"
    )


async def test_activity_logging(client: Client, worker: ExternalWorker):
    @activity.defn
    async def say_hello(name: str) -> str:
        activity.logger.info(f"Called with arg: {name}")
        return f"Hello, {name}!"

    # Create a queue, add handler to logger, call normal activity, then check
    handler = logging.handlers.QueueHandler(queue.Queue())
    activity.logger.base_logger.addHandler(handler)
    prev_level = activity.logger.base_logger.level
    activity.logger.base_logger.setLevel(logging.INFO)
    try:
        result = await _execute_workflow_with_activity(
            client, worker, say_hello, "Temporal"
        )
    finally:
        activity.logger.base_logger.removeHandler(handler)
        activity.logger.base_logger.setLevel(prev_level)
    assert result.result == "Hello, Temporal!"
    records: List[logging.LogRecord] = list(handler.queue.queue)  # type: ignore
    assert len(records) > 0
    assert records[-1].message.startswith(
        "Called with arg: Temporal ({'activity_id': '"
    )
    assert records[-1].__dict__["temporal_activity"]["activity_type"] == "say_hello"


async def test_activity_worker_shutdown(client: Client, worker: ExternalWorker):
    activity_started = asyncio.Event()

    @activity.defn
    async def wait_on_event() -> str:
        nonlocal activity_started
        activity_started.set()
        try:
            while True:
                await asyncio.sleep(0.3)
                activity.heartbeat()
        except asyncio.CancelledError:
            return "Properly cancelled"

    act_task_queue = str(uuid.uuid4())
    act_worker = Worker(client, task_queue=act_task_queue, activities=[wait_on_event])
    asyncio.create_task(act_worker.run())
    # Start workflow
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[
                KSAction(
                    execute_activity=KSExecuteActivityAction(
                        name="wait_on_event",
                        task_queue=act_task_queue,
                        heartbeat_timeout_ms=1000,
                    )
                )
            ]
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    # Wait until activity started before shutting down the worker
    await activity_started.wait()
    await act_worker.shutdown()
    assert "Properly cancelled" == await handle.result()


async def test_activity_worker_shutdown_graceful(
    client: Client, worker: ExternalWorker
):
    activity_started = asyncio.Event()

    @activity.defn
    async def wait_on_event() -> str:
        nonlocal activity_started
        activity_started.set()
        await activity.wait_for_worker_shutdown()
        return "Worker graceful shutdown"

    act_task_queue = str(uuid.uuid4())
    act_worker = Worker(
        client,
        task_queue=act_task_queue,
        activities=[wait_on_event],
        graceful_shutdown_timeout=timedelta(seconds=2),
    )
    asyncio.create_task(act_worker.run())
    # Start workflow
    handle = await client.start_workflow(
        "kitchen_sink",
        KSWorkflowParams(
            actions=[
                KSAction(
                    execute_activity=KSExecuteActivityAction(
                        name="wait_on_event", task_queue=act_task_queue
                    )
                )
            ]
        ),
        id=str(uuid.uuid4()),
        task_queue=worker.task_queue,
    )
    # Wait until activity started before shutting down the worker
    await activity_started.wait()
    await act_worker.shutdown()
    assert "Worker graceful shutdown" == await handle.result()


@activity.defn
def picklable_wait_on_event() -> str:
    activity.wait_for_worker_shutdown_sync(20)
    return "Worker graceful shutdown"


async def test_sync_activity_process_worker_shutdown_graceful(
    client: Client, worker: ExternalWorker
):
    act_task_queue = str(uuid.uuid4())
    with concurrent.futures.ProcessPoolExecutor() as executor:
        act_worker = Worker(
            client,
            task_queue=act_task_queue,
            activities=[picklable_wait_on_event],
            activity_executor=executor,
            max_concurrent_activities=default_max_concurrent_activities,
            graceful_shutdown_timeout=timedelta(seconds=2),
            shared_state_manager=_default_shared_state_manager,
        )
        asyncio.create_task(act_worker.run())

        # Start workflow
        handle = await client.start_workflow(
            "kitchen_sink",
            KSWorkflowParams(
                actions=[
                    KSAction(
                        execute_activity=KSExecuteActivityAction(
                            name="picklable_wait_on_event",
                            task_queue=act_task_queue,
                            heartbeat_timeout_ms=30000,
                        )
                    )
                ]
            ),
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )

        # Wait until activity started before shutting down the worker. Since it's
        # cross process, we'll just cheat a bit using a private var to check.
        found = False
        activity_worker = act_worker._activity_worker
        assert activity_worker
        for _ in range(10):
            await asyncio.sleep(0.2)
            found = len(activity_worker._running_activities) > 0
            if found:
                break
        assert found

        # Do shutdown
        await act_worker.shutdown()
    assert "Worker graceful shutdown" == await handle.result()


@activity.defn
def kill_my_process() -> str:
    os.kill(os.getpid(), getattr(signal, "SIGKILL", -9))
    return "does not get here"


async def test_sync_activity_process_executor_crash(
    client: Client, worker: ExternalWorker
):
    act_task_queue = str(uuid.uuid4())
    with concurrent.futures.ProcessPoolExecutor() as executor:
        act_worker = Worker(
            client,
            task_queue=act_task_queue,
            activities=[kill_my_process],
            activity_executor=executor,
            max_concurrent_activities=default_max_concurrent_activities,
            graceful_shutdown_timeout=timedelta(seconds=2),
            shared_state_manager=_default_shared_state_manager,
        )
        act_worker_task = asyncio.create_task(act_worker.run())

        # Confirm workflow failure with broken pool
        with pytest.raises(WorkflowFailureError) as workflow_err:
            await client.execute_workflow(
                "kitchen_sink",
                KSWorkflowParams(
                    actions=[
                        KSAction(
                            execute_activity=KSExecuteActivityAction(
                                name="kill_my_process",
                                task_queue=act_task_queue,
                                heartbeat_timeout_ms=30000,
                            )
                        )
                    ]
                ),
                id=str(uuid.uuid4()),
                task_queue=worker.task_queue,
            )
        assert isinstance(workflow_err.value.cause, ActivityError)
        assert isinstance(workflow_err.value.cause.cause, ApplicationError)
        assert workflow_err.value.cause.cause.type == "BrokenProcessPool"

        # Also confirm that activity worker fails unrecoverably
        with pytest.raises(RuntimeError) as worker_err:
            await asyncio.wait_for(act_worker_task, 10)
        assert str(worker_err.value) == "Activity worker failed"
        assert isinstance(worker_err.value.__cause__, BrokenProcessPool)


class AsyncActivityWrapper:
    def __init__(self) -> None:
        self._info: Optional[activity.Info] = None
        self._info_set = asyncio.Event()

    @activity.defn
    async def run(self) -> Optional[str]:
        self._info = activity.info()
        self._info_set.set()
        activity.raise_complete_async()

    async def wait_info(self) -> activity.Info:
        await asyncio.wait_for(self._info_set.wait(), timeout=3)
        self._info_set.clear()
        assert self._info
        return self._info

    def async_handle(self, client: Client, use_task_token: bool) -> AsyncActivityHandle:
        assert self._info
        if use_task_token:
            return client.get_async_activity_handle(task_token=self._info.task_token)
        return client.get_async_activity_handle(
            workflow_id=self._info.workflow_id,
            run_id=self._info.workflow_run_id,
            activity_id=self._info.activity_id,
        )


@pytest.mark.parametrize("use_task_token", [True, False])
async def test_activity_async_success(
    client: Client, worker: ExternalWorker, use_task_token: bool
):
    # Start task, wait for info, complete with value, wait on workflow
    wrapper = AsyncActivityWrapper()
    task = asyncio.create_task(
        _execute_workflow_with_activity(client, worker, wrapper.run)
    )
    await wrapper.wait_info()
    await wrapper.async_handle(client, use_task_token).complete("some value")
    assert "some value" == (await task).result

    # Do again with a None value
    wrapper = AsyncActivityWrapper()
    task = asyncio.create_task(
        _execute_workflow_with_activity(client, worker, wrapper.run)
    )
    await wrapper.wait_info()
    await wrapper.async_handle(client, use_task_token).complete(None)
    assert (await task).result is None


@pytest.mark.parametrize("use_task_token", [True, False])
async def test_activity_async_heartbeat_and_fail(
    client: Client,
    worker: ExternalWorker,
    env: WorkflowEnvironment,
    use_task_token: bool,
):
    if env.supports_time_skipping:
        pytest.skip("https://github.com/temporalio/sdk-java/issues/2459")

    wrapper = AsyncActivityWrapper()
    # Start task w/ max attempts 2, wait for info, send heartbeat, fail
    task = asyncio.create_task(
        _execute_workflow_with_activity(
            client, worker, wrapper.run, retry_max_attempts=2
        )
    )
    info = await wrapper.wait_info()
    assert info.attempt == 1
    await wrapper.async_handle(client, use_task_token).heartbeat("heartbeat details")
    await wrapper.async_handle(client, use_task_token).fail(
        ApplicationError("err message", "err details")
    )
    # Since we know it will retry, wait for the info again
    info = await wrapper.wait_info()
    # Confirm the heartbeat details and attempt
    assert info.attempt == 2
    assert list(info.heartbeat_details) == ["heartbeat details"]
    # Fail again which won't retry
    await wrapper.async_handle(client, use_task_token).fail(
        ApplicationError("err message 2", "err details 2")
    )
    with pytest.raises(WorkflowFailureError) as err:
        await task
    assert isinstance(err.value.cause, ActivityError)
    assert isinstance(err.value.cause.cause, ApplicationError)
    assert err.value.cause.cause.message == "err message 2"
    assert list(err.value.cause.cause.details) == ["err details 2"]


@pytest.mark.parametrize("use_task_token", [True, False])
async def test_activity_async_cancel(
    client: Client, worker: ExternalWorker, use_task_token: bool
):
    wrapper = AsyncActivityWrapper()
    # Start task, wait for info, cancel, wait on workflow
    task = asyncio.create_task(
        _execute_workflow_with_activity(
            client, worker, wrapper.run, cancel_after_ms=50, wait_for_cancellation=True
        )
    )
    await wrapper.wait_info()
    # Sleep 2s before trying to cancel
    await asyncio.sleep(2)
    await wrapper.async_handle(client, use_task_token).report_cancellation(
        "cancel details"
    )
    with pytest.raises(WorkflowFailureError) as err:
        await task
    assert isinstance(err.value.cause, ActivityError)
    assert isinstance(err.value.cause.cause, CancelledError)
    assert list(err.value.cause.cause.details) == ["cancel details"]


some_context_var: ContextVar[str] = ContextVar("some_context_var", default="unset")


class ContextVarInterceptor(Interceptor):
    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return super().intercept_activity(ContextVarActivityInboundInterceptor(next))


class ContextVarActivityInboundInterceptor(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        some_context_var.set("some value!")
        return await super().execute_activity(input)


async def test_sync_activity_contextvars(client: Client, worker: ExternalWorker):
    @activity.defn
    def some_activity() -> str:
        return f"context var: {some_context_var.get()}"

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=default_max_concurrent_activities
    ) as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            some_activity,
            worker_config={
                "activity_executor": executor,
                "interceptors": [ContextVarInterceptor()],
            },
        )
    assert result.result == "context var: some value!"


@activity.defn
async def local_without_schedule_to_close_activity() -> str:
    return "some-activity"


@workflow.defn(sandboxed=False)
class LocalActivityWithoutScheduleToCloseWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.execute_local_activity(
            local_without_schedule_to_close_activity,
            start_to_close_timeout=timedelta(minutes=2),
        )


async def test_activity_local_without_schedule_to_close(client: Client):
    task_queue = f"tq-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        activities=[local_without_schedule_to_close_activity],
        workflows=[LocalActivityWithoutScheduleToCloseWorkflow],
    ):
        await client.execute_workflow(
            LocalActivityWithoutScheduleToCloseWorkflow.run,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=task_queue,
            # This emulates what Go SDK would do
            execution_timeout=timedelta(seconds=0),
        )


@dataclass
class DynActivityValue:
    some_string: str


# External so it is picklable
@activity.defn(dynamic=True)
def sync_dyn_activity(args: Sequence[RawValue]) -> DynActivityValue:
    assert len(args) == 2
    arg1 = activity.payload_converter().from_payload(args[0].payload, DynActivityValue)
    assert isinstance(arg1, DynActivityValue)
    arg2 = activity.payload_converter().from_payload(args[1].payload, DynActivityValue)
    assert isinstance(arg1, DynActivityValue)
    return DynActivityValue(
        f"{activity.info().activity_type} - {arg1.some_string} - {arg2.some_string}"
    )


async def test_activity_dynamic(client: Client, worker: ExternalWorker):
    @activity.defn(dynamic=True)
    async def async_dyn_activity(args: Sequence[RawValue]) -> DynActivityValue:
        return sync_dyn_activity(args)

    result = await _execute_workflow_with_activity(
        client,
        worker,
        async_dyn_activity,
        DynActivityValue("val1"),
        DynActivityValue("val2"),
        activity_name_override="some-activity-name",
        result_type_override=DynActivityValue,
    )
    assert result.result == DynActivityValue("some-activity-name - val1 - val2")


async def test_sync_activity_dynamic_thread(client: Client, worker: ExternalWorker):
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=default_max_concurrent_activities
    ) as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            sync_dyn_activity,
            DynActivityValue("val1"),
            DynActivityValue("val2"),
            worker_config={"activity_executor": executor},
            activity_name_override="some-activity-name",
            result_type_override=DynActivityValue,
        )
        assert result.result == DynActivityValue("some-activity-name - val1 - val2")


async def test_sync_activity_dynamic_process(client: Client, worker: ExternalWorker):
    with concurrent.futures.ProcessPoolExecutor() as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            sync_dyn_activity,
            DynActivityValue("val1"),
            DynActivityValue("val2"),
            worker_config={"activity_executor": executor},
            activity_name_override="some-activity-name",
            result_type_override=DynActivityValue,
        )
        assert result.result == DynActivityValue("some-activity-name - val1 - val2")


async def test_activity_dynamic_duplicate(client: Client, worker: ExternalWorker):
    @activity.defn(dynamic=True)
    async def dyn_activity_1(args: Sequence[RawValue]) -> None:
        pass

    @activity.defn(dynamic=True)
    async def dyn_activity_2(args: Sequence[RawValue]) -> None:
        pass

    with pytest.raises(TypeError) as err:
        await _execute_workflow_with_activity(
            client, worker, dyn_activity_1, additional_activities=[dyn_activity_2]
        )
    assert "More than one dynamic activity" in str(err.value)


@dataclass
class _ActivityResult:
    act_task_queue: str
    result: Any
    handle: WorkflowHandle


async def _execute_workflow_with_activity(
    client: Client,
    worker: ExternalWorker,
    fn: Callable,
    *args: Any,
    count: Optional[int] = None,
    index_as_arg: Optional[bool] = None,
    schedule_to_close_timeout_ms: Optional[int] = None,
    start_to_close_timeout_ms: Optional[int] = None,
    schedule_to_start_timeout_ms: Optional[int] = None,
    cancel_after_ms: Optional[int] = None,
    wait_for_cancellation: Optional[bool] = None,
    heartbeat_timeout_ms: Optional[int] = None,
    retry_max_attempts: Optional[int] = None,
    non_retryable_error_types: Optional[Sequence[str]] = None,
    worker_config: WorkerConfig = {},
    on_complete: Optional[Callable[[], None]] = None,
    activity_name_override: Optional[str] = None,
    result_type_override: Optional[Type] = None,
    additional_activities: List[Callable] = [],
) -> _ActivityResult:
    worker_config["client"] = client
    worker_config["task_queue"] = str(uuid.uuid4())
    worker_config["activities"] = [fn] + additional_activities
    worker_config["shared_state_manager"] = _default_shared_state_manager
    if not worker_config.get("max_concurrent_activities"):
        worker_config["max_concurrent_activities"] = default_max_concurrent_activities
    async with Worker(**worker_config):
        try:
            handle = await client.start_workflow(
                "kitchen_sink",
                KSWorkflowParams(
                    actions=[
                        KSAction(
                            execute_activity=KSExecuteActivityAction(
                                name=activity_name_override or fn.__name__,
                                task_queue=worker_config["task_queue"],
                                args=args,
                                count=count,
                                index_as_arg=index_as_arg,
                                schedule_to_close_timeout_ms=schedule_to_close_timeout_ms,
                                start_to_close_timeout_ms=start_to_close_timeout_ms,
                                schedule_to_start_timeout_ms=schedule_to_start_timeout_ms,
                                cancel_after_ms=cancel_after_ms,
                                wait_for_cancellation=wait_for_cancellation,
                                heartbeat_timeout_ms=heartbeat_timeout_ms,
                                retry_max_attempts=retry_max_attempts,
                                non_retryable_error_types=non_retryable_error_types,
                            )
                        )
                    ]
                ),
                id=str(uuid.uuid4()),
                task_queue=worker.task_queue,
                result_type=result_type_override,
            )
            return _ActivityResult(
                act_task_queue=worker_config["task_queue"],
                result=await handle.result(),
                handle=handle,
            )
        finally:
            if on_complete:
                on_complete()


def assert_activity_error(err: WorkflowFailureError) -> BaseException:
    assert isinstance(err.cause, ActivityError)
    assert err.cause.__cause__
    return err.cause.__cause__


def assert_activity_application_error(
    err: WorkflowFailureError,
) -> ApplicationError:
    ret = assert_activity_error(err)
    assert isinstance(ret, ApplicationError)
    return ret


class CustomLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self._trace_identifiers = 0

    def emit(self, record: logging.LogRecord) -> None:
        if (
            hasattr(record, "__temporal_error_identifier")
            and getattr(record, "__temporal_error_identifier") == "ActivityFailure"
        ):
            assert record.msg.startswith("Completing activity as failed")
            self._trace_identifiers += 1
        return None


async def test_activity_failure_trace_identifier(
    client: Client, worker: ExternalWorker
):
    @activity.defn
    async def raise_error():
        raise RuntimeError("oh no!")

    handler = CustomLogHandler()
    activity.logger.base_logger.addHandler(handler)

    try:
        with pytest.raises(WorkflowFailureError) as err:
            await _execute_workflow_with_activity(client, worker, raise_error)
        assert (
            str(assert_activity_application_error(err.value)) == "RuntimeError: oh no!"
        )
        assert handler._trace_identifiers == 1

    finally:
        activity.logger.base_logger.removeHandler(CustomLogHandler())


async def test_activity_heartbeat_context(client: Client, worker: ExternalWorker):
    @activity.defn
    async def heartbeat():
        if activity.info().attempt == 1:
            context: activity._Context = activity._Context.current()

            def heartbeat_task():
                async def h():
                    if context.heartbeat is not None:
                        context.heartbeat("Some detail")

                asyncio.run(h())

            thread = threading.Thread(target=heartbeat_task)
            thread.start()
            thread.join()
            raise RuntimeError("oh no!")
        else:
            assert len(activity.info().heartbeat_details) == 1
            return "details: " + activity.info().heartbeat_details[0]

    result = await _execute_workflow_with_activity(
        client, worker, heartbeat, retry_max_attempts=2
    )
    assert result.result == "details: Some detail"

async def test_activity_reset(client: Client, worker: ExternalWorker):

    @activity.defn
    async def reset_activity() -> None:

        await client.workflow_service.reset_activity(temporalio.api.workflowservice.v1.ResetActivityRequest(
            namespace=client.namespace,
            execution=temporalio.api.common.v1.WorkflowExecution(
                workflow_id=activity.info().workflow_id,
                run_id=activity.info().workflow_run_id,
            ),
            id=activity.info().activity_id,
            ))
        reset = False
        for _ in range(5):
            try:
                if reset:
                    return None
                await asyncio.sleep(1)
            except Exception as e:
                activity.logger.warning("Exception: ", e)
                reset = True
                raise

        assert False

    await _execute_workflow_with_activity(
        client, worker, reset_activity
    )


