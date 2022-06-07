import asyncio
import concurrent.futures
import logging
import logging.handlers
import multiprocessing
import queue
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, List, Optional, Tuple

import pytest

from temporalio import activity
from temporalio.client import (
    AsyncActivityHandle,
    Client,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    TimeoutError,
    TimeoutType,
)
from temporalio.worker import (
    ActivityInboundInterceptor,
    ActivityOutboundInterceptor,
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

_default_shared_state_manager = SharedStateManager.create_from_multiprocessing(
    multiprocessing.Manager()
)


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


async def test_activity_info(client: Client, worker: ExternalWorker):
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
    assert info.heartbeat_details == []
    assert info.heartbeat_timeout == timedelta()
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


async def test_sync_activity_thread(client: Client, worker: ExternalWorker):
    @activity.defn
    def some_activity() -> str:
        return f"activity name: {activity.info().activity_type}"

    with concurrent.futures.ThreadPoolExecutor() as executor:
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
    with concurrent.futures.ProcessPoolExecutor() as executor:
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
    assert str(assert_activity_application_error(err.value)) == "oh no!"


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
    assert str(assert_activity_application_error(err.value)) == "oh no!"


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
                await asyncio.sleep(0.1)
                activity.heartbeat()
        except asyncio.CancelledError:
            return "Got cancelled error, cancelled? " + str(activity.is_cancelled())

    result = await _execute_workflow_with_activity(
        client,
        worker,
        wait_cancel,
        cancel_after_ms=100,
        wait_for_cancellation=True,
        heartbeat_timeout_ms=1000,
    )
    assert result.result == "Got cancelled error, cancelled? True"


async def test_activity_cancel_throw(client: Client, worker: ExternalWorker):
    @activity.defn
    async def wait_cancel() -> str:
        while True:
            await asyncio.sleep(0.1)
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
    # TODO(cretz): This is a side effect of Go where returning the activity
    # cancel looks like a workflow cancel. Change assertion if/when on another
    # lang.
    assert isinstance(err.value.cause, CancelledError)


async def test_sync_activity_thread_cancel(client: Client, worker: ExternalWorker):
    @activity.defn
    def wait_cancel() -> str:
        while not activity.is_cancelled():
            time.sleep(1)
            activity.heartbeat()
        return "Cancelled"

    with concurrent.futures.ThreadPoolExecutor() as executor:
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


@activity.defn
def picklable_activity_wait_cancel() -> str:
    while not activity.is_cancelled():
        time.sleep(1)
        activity.heartbeat()
    return "Cancelled"


# TODO(cretz): Investigate
@pytest.mark.skipif(
    sys.version_info < (3, 10) and sys.platform.startswith("darwin"),
    reason="CI on 3.7 with macOS fails here intermittently due to GC issues",
)
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
    assert str(assert_activity_application_error(err.value)) == (
        "Activity function wrong_activity is not registered on this worker, "
        "available activities: say_hello"
    )


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
    assert str(timeout) == "activity timeout"
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
        123,
    )
    # We called with the wrong non-dataclass type, but since we don't strictly
    # check non-data-types, we don't perform any validation there
    assert (
        result.result
        == "param1: <class 'tests.worker.test_activity.SomeClass2'>, param2: <class 'int'>"
    )
    assert activity_param1 == SomeClass2(foo="str1", bar=SomeClass1(foo=123))


async def test_activity_heartbeat_details(client: Client, worker: ExternalWorker):
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
    client: Client, worker: ExternalWorker
):
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
    assert str(timeout) == "activity timeout"
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
    client: Client, worker: ExternalWorker
):
    with concurrent.futures.ThreadPoolExecutor() as executor:
        result = await _execute_workflow_with_activity(
            client,
            worker,
            picklable_heartbeat_details_activity,
            retry_max_attempts=2,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "attempt: 1, attempt: 2"


async def test_sync_activity_process_heartbeat_details(
    client: Client, worker: ExternalWorker
):
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
    assert "Can't pickle" in str(assert_activity_application_error(err.value))


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
    assert str(assert_activity_application_error(err.value)) == "Do not retry me"


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
    assert records[-1].__dict__["activity_info"].activity_type == "say_hello"


async def test_activity_worker_shutdown(client: Client, worker: ExternalWorker):
    activity_started = asyncio.Event()

    @activity.defn
    async def wait_on_event() -> str:
        nonlocal activity_started
        activity_started.set()
        try:
            while True:
                await asyncio.sleep(0.1)
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


async def test_activity_interceptor(client: Client, worker: ExternalWorker):
    @activity.defn
    async def say_hello(name: str) -> str:
        # Get info and record a heartbeat
        activity.info()
        activity.heartbeat("some details!")
        return f"Hello, {name}!"

    interceptor = TracingWorkerInterceptor()
    result = await _execute_workflow_with_activity(
        client,
        worker,
        say_hello,
        "Temporal",
        worker_config={"interceptors": [interceptor]},
    )
    assert result.result == "Hello, Temporal!"
    # Since "info" is called multiple times (called in logger to provide
    # context), we just get traces by key
    traces = {trace[0]: trace[1] for trace in interceptor.traces}
    assert traces["activity.inbound.execute_activity"].args[0] == "Temporal"
    assert "activity.outbound.info" in traces
    assert traces["activity.outbound.heartbeat"][0] == "some details!"


class AsyncActivityWrapper:
    def __init__(self) -> None:
        self._info: Optional[activity.Info] = None
        self._info_set = asyncio.Event()

    @activity.defn
    async def run(self) -> str:
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
    wrapper = AsyncActivityWrapper()
    # Start task, wait for info, complete with value, wait on workflow
    task = asyncio.create_task(
        _execute_workflow_with_activity(client, worker, wrapper.run)
    )
    await wrapper.wait_info()
    await wrapper.async_handle(client, use_task_token).complete("some value")
    assert "some value" == (await task).result


@pytest.mark.parametrize("use_task_token", [True, False])
async def test_activity_async_heartbeat_and_fail(
    client: Client, worker: ExternalWorker, use_task_token: bool
):
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
    assert isinstance(err.value.cause, CancelledError)
    assert list(err.value.cause.details) == ["cancel details"]


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
    non_retryable_error_types: Optional[Iterable[str]] = None,
    worker_config: WorkerConfig = {},
    on_complete: Optional[Callable[[], None]] = None,
    activity_name_override: Optional[str] = None,
) -> _ActivityResult:
    worker_config["client"] = client
    worker_config["task_queue"] = str(uuid.uuid4())
    worker_config["activities"] = [fn]
    worker_config["shared_state_manager"] = _default_shared_state_manager
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


class TracingWorkerInterceptor(Interceptor):
    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        self.traces: List[Tuple[str, Any]] = []
        return TracingWorkerActivityInboundInterceptor(self, next)


class TracingWorkerActivityInboundInterceptor(ActivityInboundInterceptor):
    def __init__(
        self,
        parent: TracingWorkerInterceptor,
        next: ActivityInboundInterceptor,
    ) -> None:
        super().__init__(next)
        self._parent = parent

    def init(self, outbound: ActivityOutboundInterceptor) -> None:
        return super().init(
            TracingWorkerActivityOutboundInterceptor(self._parent, outbound)
        )

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        self._parent.traces.append(("activity.inbound.execute_activity", input))
        return await super().execute_activity(input)


class TracingWorkerActivityOutboundInterceptor(ActivityOutboundInterceptor):
    def __init__(
        self,
        parent: TracingWorkerInterceptor,
        next: ActivityOutboundInterceptor,
    ) -> None:
        super().__init__(next)
        self._parent = parent

    def info(self) -> activity.Info:
        self._parent.traces.append(("activity.outbound.info", None))
        return super().info()

    def heartbeat(self, *details: Any) -> None:
        self._parent.traces.append(("activity.outbound.heartbeat", details))
        return super().heartbeat(*details)
