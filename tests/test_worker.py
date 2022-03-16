import asyncio
import concurrent.futures
import logging
import logging.handlers
import multiprocessing
import queue
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, List, Optional, Tuple

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

_default_shared_state_manager = (
    temporalio.worker.SharedStateManager.create_from_multiprocessing(
        multiprocessing.Manager()
    )
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
    assert str(assert_activity_application_error(err.value)) == "oh no!"


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
    assert str(assert_activity_application_error(err.value)) == "oh no!"


async def test_activity_bad_params(client: temporalio.client.Client, worker: Worker):
    async def say_hello(name: str) -> str:
        return f"Hello, {name}!"

    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await _execute_workflow_with_activity(client, worker, say_hello)
    assert str(assert_activity_application_error(err.value)).endswith(
        "missing 1 required positional argument: 'name'"
    )


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
                temporalio.activity.is_cancelled()
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
        while not temporalio.activity.is_cancelled():
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
    while not temporalio.activity.is_cancelled():
        time.sleep(0.3)
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
            heartbeat_timeout_ms=30000,
            worker_config={"activity_executor": executor},
        )
    assert result.result == "Cancelled"


async def test_activity_does_not_exist(
    client: temporalio.client.Client, worker: Worker
):
    async def say_hello(name: str) -> str:
        return f"Hello, {name}!"

    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        act_task_queue = str(uuid.uuid4())
        async with temporalio.worker.Worker(
            client, task_queue=act_task_queue, activities={"say_hello": say_hello}
        ):
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


async def test_max_concurrent_activities(
    client: temporalio.client.Client, worker: Worker
):
    seen_indexes: List[int] = []
    complete_activities_event = asyncio.Event()

    async def some_activity(index: int) -> str:
        seen_indexes.append(index)
        # Wait here to hold up the activity
        await complete_activities_event.wait()
        return ""

    # Only allow 42 activities, but try to execute 43. Make a short schedule to
    # start timeout but a long schedule to close timeout.
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
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
    assert isinstance(timeout, temporalio.exceptions.TimeoutError)
    assert str(timeout) == "activity timeout"
    assert timeout.type == temporalio.exceptions.TimeoutType.SCHEDULE_TO_START


@dataclass
class SomeClass1:
    foo: int


@dataclass
class SomeClass2:
    foo: str
    bar: Optional[SomeClass1] = None


async def test_activity_type_hints(client: temporalio.client.Client, worker: Worker):
    activity_param1: SomeClass2

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
        == "param1: <class 'tests.test_worker.SomeClass2'>, param2: <class 'int'>"
    )
    assert activity_param1 == SomeClass2(foo="str1", bar=SomeClass1(foo=123))


async def test_activity_heartbeat_details(
    client: temporalio.client.Client, worker: Worker
):
    async def some_activity() -> str:
        info = temporalio.activity.info()
        count = int(next(iter(info.heartbeat_details))) if info.heartbeat_details else 0
        temporalio.activity.logger.debug(
            "Changing count from %s to %s", count, count + 9
        )
        count += 9
        temporalio.activity.heartbeat(count)
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
    client: temporalio.client.Client, worker: Worker
):
    async def some_activity() -> str:
        temporalio.activity.heartbeat(NotSerializableValue())
        # Since the above fails, it will cause this task to be cancelled on the
        # next event loop iteration, so we sleep for a short time to allow that
        # iteration to occur
        await asyncio.sleep(0.05)
        return "Should not get here"

    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await _execute_workflow_with_activity(client, worker, some_activity)
    assert str(assert_activity_application_error(err.value)).endswith(
        "has no known converter"
    )


async def test_activity_heartbeat_details_timeout(
    client: temporalio.client.Client, worker: Worker
):
    async def some_activity() -> str:
        temporalio.activity.heartbeat("some details!")
        await asyncio.sleep(3)
        return "Should not get here"

    # Have a 1s heartbeat timeout that we won't meet with a second heartbeat
    # then check the timeout's details
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await _execute_workflow_with_activity(
            client, worker, some_activity, heartbeat_timeout_ms=1000
        )
    timeout = assert_activity_error(err.value)
    assert isinstance(timeout, temporalio.exceptions.TimeoutError)
    assert str(timeout) == "activity timeout"
    assert timeout.type == temporalio.exceptions.TimeoutType.HEARTBEAT
    assert list(timeout.last_heartbeat_details) == ["some details!"]


def picklable_heartbeat_details_activity() -> str:
    info = temporalio.activity.info()
    some_list: List[str] = (
        next(iter(info.heartbeat_details)) if info.heartbeat_details else []
    )
    some_list.append(f"attempt: {info.attempt}")
    temporalio.activity.logger.debug("Heartbeating with value: %s", some_list)
    temporalio.activity.heartbeat(some_list)
    if len(some_list) < 2:
        raise RuntimeError(f"Try again, list contains: {some_list}")
    return ", ".join(some_list)


async def test_sync_activity_thread_heartbeat_details(
    client: temporalio.client.Client, worker: Worker
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
    client: temporalio.client.Client, worker: Worker
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


def picklable_activity_non_pickable_heartbeat_details() -> str:
    temporalio.activity.heartbeat(lambda: "cannot pickle lambda by default")
    return "Should not get here"


async def test_sync_activity_process_non_picklable_heartbeat_details(
    client: temporalio.client.Client, worker: Worker
):
    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        with concurrent.futures.ProcessPoolExecutor() as executor:
            await _execute_workflow_with_activity(
                client,
                worker,
                picklable_activity_non_pickable_heartbeat_details,
                worker_config={"activity_executor": executor},
            )
    assert "Can't pickle" in str(assert_activity_application_error(err.value))


async def test_activity_error_non_retryable(
    client: temporalio.client.Client, worker: Worker
):
    async def some_activity():
        if temporalio.activity.info().attempt < 2:
            raise temporalio.exceptions.ApplicationError(
                "Retry me", non_retryable=False
            )
        # We'll test error details while we're here
        raise temporalio.exceptions.ApplicationError(
            "Do not retry me", "detail1", 123, non_retryable=True
        )

    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
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
    client: temporalio.client.Client, worker: Worker
):
    async def some_activity():
        if temporalio.activity.info().attempt < 2:
            raise temporalio.exceptions.ApplicationError(
                "Retry me", type="Can retry me"
            )
        raise temporalio.exceptions.ApplicationError(
            "Do not retry me", type="Cannot retry me"
        )

    with pytest.raises(temporalio.client.WorkflowFailureError) as err:
        await _execute_workflow_with_activity(
            client,
            worker,
            some_activity,
            retry_max_attempts=100,
            non_retryable_error_types=["Cannot retry me"],
        )
    assert str(assert_activity_application_error(err.value)) == "Do not retry me"


async def test_activity_logging(client: temporalio.client.Client, worker: Worker):
    async def say_hello(name: str) -> str:
        temporalio.activity.logger.info(f"Called with arg: {name}")
        return f"Hello, {name}!"

    # Create a queue, add handler to logger, call normal activity, then check
    handler = logging.handlers.QueueHandler(queue.Queue())
    temporalio.activity.logger.base_logger.addHandler(handler)
    prev_level = temporalio.activity.logger.base_logger.level
    temporalio.activity.logger.base_logger.setLevel(logging.INFO)
    try:
        result = await _execute_workflow_with_activity(
            client, worker, say_hello, "Temporal"
        )
    finally:
        temporalio.activity.logger.base_logger.removeHandler(handler)
        temporalio.activity.logger.base_logger.setLevel(prev_level)
    assert result.result == "Hello, Temporal!"
    records: List[logging.LogRecord] = list(handler.queue.queue)  # type: ignore
    assert len(records) > 0
    assert records[-1].message.startswith(
        "Called with arg: Temporal ({'activity_id': '"
    )
    assert records[-1].__dict__["activity_info"].activity_type == "say_hello"


async def test_activity_worker_shutdown(
    client: temporalio.client.Client, worker: Worker
):
    activity_started = asyncio.Event()

    async def wait_on_event() -> str:
        nonlocal activity_started
        activity_started.set()
        try:
            while True:
                await asyncio.sleep(0.1)
                temporalio.activity.heartbeat()
        except asyncio.CancelledError:
            return "Properly cancelled"

    act_task_queue = str(uuid.uuid4())
    act_worker = temporalio.worker.Worker(
        client, task_queue=act_task_queue, activities={"wait_on_event": wait_on_event}
    )
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
    client: temporalio.client.Client, worker: Worker
):
    activity_started = asyncio.Event()

    async def wait_on_event() -> str:
        nonlocal activity_started
        activity_started.set()
        await temporalio.activity.wait_for_worker_shutdown()
        return "Worker graceful shutdown"

    act_task_queue = str(uuid.uuid4())
    act_worker = temporalio.worker.Worker(
        client,
        task_queue=act_task_queue,
        activities={"wait_on_event": wait_on_event},
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


def picklable_wait_on_event() -> str:
    temporalio.activity.wait_for_worker_shutdown_sync(5)
    return "Worker graceful shutdown"


async def test_sync_activity_process_worker_shutdown_graceful(
    client: temporalio.client.Client, worker: Worker
):
    act_task_queue = str(uuid.uuid4())
    with concurrent.futures.ProcessPoolExecutor() as executor:
        act_worker = temporalio.worker.Worker(
            client,
            task_queue=act_task_queue,
            activities={"picklable_wait_on_event": picklable_wait_on_event},
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
                            heartbeat_timeout_ms=1000,
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
        for _ in range(10):
            await asyncio.sleep(0.2)
            found = len(act_worker._running_activities) > 0
            if found:
                break
        assert found

        # Do shutdown
        await act_worker.shutdown()
    assert "Worker graceful shutdown" == await handle.result()


async def test_activity_interceptor(client: temporalio.client.Client, worker: Worker):
    async def say_hello(name: str) -> str:
        # Get info and record a heartbeat
        temporalio.activity.info()
        temporalio.activity.heartbeat("some details!")
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
    worker_config: temporalio.worker.WorkerConfig = {},
    on_complete: Optional[Callable[[], None]] = None,
) -> _ActivityResult:
    worker_config["client"] = client
    worker_config["task_queue"] = str(uuid.uuid4())
    worker_config["activities"] = {fn.__name__: fn}
    worker_config["shared_state_manager"] = _default_shared_state_manager
    async with temporalio.worker.Worker(**worker_config):
        try:
            handle = await client.start_workflow(
                "kitchen_sink",
                KSWorkflowParams(
                    actions=[
                        KSAction(
                            execute_activity=KSExecuteActivityAction(
                                name=fn.__name__,
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


def assert_activity_error(err: temporalio.client.WorkflowFailureError) -> BaseException:
    assert isinstance(err.cause, temporalio.exceptions.ActivityError)
    assert err.cause.__cause__
    return err.cause.__cause__


def assert_activity_application_error(
    err: temporalio.client.WorkflowFailureError,
) -> temporalio.exceptions.ApplicationError:
    ret = assert_activity_error(err)
    assert isinstance(ret, temporalio.exceptions.ApplicationError)
    return ret


class TracingWorkerInterceptor(temporalio.worker.Interceptor):
    def intercept_activity(
        self, next: temporalio.worker.ActivityInboundInterceptor
    ) -> temporalio.worker.ActivityInboundInterceptor:
        self.traces: List[Tuple[str, Any]] = []
        return TracingWorkerActivityInboundInterceptor(self, next)


class TracingWorkerActivityInboundInterceptor(
    temporalio.worker.ActivityInboundInterceptor
):
    def __init__(
        self,
        parent: TracingWorkerInterceptor,
        next: temporalio.worker.ActivityInboundInterceptor,
    ) -> None:
        super().__init__(next)
        self._parent = parent

    def init(self, outbound: temporalio.worker.ActivityOutboundInterceptor) -> None:
        return super().init(
            TracingWorkerActivityOutboundInterceptor(self._parent, outbound)
        )

    async def execute_activity(
        self, input: temporalio.worker.ExecuteActivityInput
    ) -> Any:
        self._parent.traces.append(("activity.inbound.execute_activity", input))
        return await super().execute_activity(input)


class TracingWorkerActivityOutboundInterceptor(
    temporalio.worker.ActivityOutboundInterceptor
):
    def __init__(
        self,
        parent: TracingWorkerInterceptor,
        next: temporalio.worker.ActivityOutboundInterceptor,
    ) -> None:
        super().__init__(next)
        self._parent = parent

    def info(self) -> temporalio.activity.Info:
        self._parent.traces.append(("activity.outbound.info", None))
        return super().info()

    def heartbeat(self, *details: Any) -> None:
        self._parent.traces.append(("activity.outbound.heartbeat", details))
        return super().heartbeat(*details)
