from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

import pytest

import temporalio.worker._worker
from temporalio import activity, workflow
from temporalio.client import BuildIdOpAddNewDefault, Client, TaskReachabilityType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    FixedSizeSlotSupplier,
    ResourceBasedSlotConfig,
    ResourceBasedSlotSupplier,
    ResourceBasedTunerConfig,
    Worker,
    WorkerTuner,
)
from temporalio.workflow import VersioningIntent
from tests.helpers import new_worker, worker_versioning_enabled


def test_load_default_worker_binary_id():
    # Just run it twice and confirm it didn't change
    val1 = temporalio.worker._worker.load_default_build_id(memoize=False)
    val2 = temporalio.worker._worker.load_default_build_id(memoize=False)
    assert val1 == val2


@activity.defn
async def never_run_activity() -> None:
    raise NotImplementedError


@workflow.defn
class NeverRunWorkflow:
    @workflow.run
    async def run(self) -> None:
        raise NotImplementedError


async def test_worker_fatal_error_run(client: Client):
    # Run worker with injected workflow poll error
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            inj.workflow.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
            await worker.run()
    assert str(err.value) == "Workflow worker failed"
    assert err.value.__cause__ and str(err.value.__cause__) == "OH NO"

    # Run worker with injected activity poll error
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            inj.activity.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
            await worker.run()
    assert str(err.value) == "Activity worker failed"
    assert err.value.__cause__ and str(err.value.__cause__) == "OH NO"

    # Run worker with them both injected (was causing warning for not retrieving
    # the second error)
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            inj.workflow.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
            inj.activity.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
            await worker.run()
    assert str(err.value).endswith("worker failed")
    assert err.value.__cause__ and str(err.value.__cause__) == "OH NO"


async def test_worker_fatal_error_with(client: Client):
    # Start the worker, wait a short bit, fail it, wait for long time (will be
    # cancelled)
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            async with worker:
                await asyncio.sleep(0.1)
                inj.workflow.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
                await asyncio.sleep(1000)
    assert str(err.value) == "Workflow worker failed"
    assert err.value.__cause__ and str(err.value.__cause__) == "OH NO"

    # Raise inside the async with and confirm it works
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            async with worker:
                raise RuntimeError("IN WITH")
    assert str(err.value) == "IN WITH"

    # Demonstrate that inner re-thrown failure swallows worker failure
    worker = create_worker(client)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            async with worker:
                inj.workflow.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
                try:
                    await asyncio.sleep(1000)
                except BaseException as inner_err:
                    raise RuntimeError("Caught cancel") from inner_err
    assert str(err.value) == "Caught cancel"
    assert err.value.__cause__ and type(err.value.__cause__) is asyncio.CancelledError


async def test_worker_fatal_error_callback(client: Client):
    callback_err: Optional[BaseException] = None

    async def on_fatal_error(exc: BaseException) -> None:
        nonlocal callback_err
        callback_err = exc

    worker = create_worker(client, on_fatal_error)
    with pytest.raises(RuntimeError) as err:
        with WorkerFailureInjector(worker) as inj:
            async with worker:
                await asyncio.sleep(0.1)
                inj.workflow.poll_fail_queue.put_nowait(RuntimeError("OH NO"))
                await asyncio.sleep(1000)
    assert err.value is callback_err


async def test_worker_cancel_run(client: Client):
    worker = create_worker(client)
    assert not worker.is_running and not worker.is_shutdown
    run_task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.3)
    assert worker.is_running and not worker.is_shutdown
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert not worker.is_running and worker.is_shutdown


@activity.defn
async def say_hello(name: str) -> str:
    return f"Hello, {name}!"


@workflow.defn
class WaitOnSignalWorkflow:
    def __init__(self) -> None:
        self._last_signal = "<none>"

    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: self._last_signal == "finish")
        await workflow.execute_activity(
            say_hello,
            "hi",
            versioning_intent=VersioningIntent.DEFAULT,
            start_to_close_timeout=timedelta(seconds=5),
        )

    @workflow.signal
    def my_signal(self, value: str) -> None:
        self._last_signal = value
        workflow.logger.info(f"Signal: {value}")


async def test_worker_versioning(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Java test server does not support worker versioning")
    if not await worker_versioning_enabled(client):
        pytest.skip("This server does not have worker versioning enabled")

    task_queue = f"worker-versioning-{uuid.uuid4()}"
    await client.update_worker_build_id_compatibility(
        task_queue, BuildIdOpAddNewDefault("1.0")
    )

    async with new_worker(
        client,
        WaitOnSignalWorkflow,
        activities=[say_hello],
        task_queue=task_queue,
        build_id="1.0",
        use_worker_versioning=True,
    ):
        wf1 = await client.start_workflow(
            WaitOnSignalWorkflow.run,
            id=f"worker-versioning-1-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        # Sleep for a beat, otherwise it's possible for new workflow to start on 2.0
        await asyncio.sleep(0.1)
        await client.update_worker_build_id_compatibility(
            task_queue, BuildIdOpAddNewDefault("2.0")
        )
        wf2 = await client.start_workflow(
            WaitOnSignalWorkflow.run,
            id=f"worker-versioning-2-{uuid.uuid4()}",
            task_queue=task_queue,
        )
        async with new_worker(
            client,
            WaitOnSignalWorkflow,
            activities=[say_hello],
            task_queue=task_queue,
            build_id="2.0",
            use_worker_versioning=True,
        ):
            # Confirm reachability type parameter is respected. If it wasn't, list would have
            # `OPEN_WORKFLOWS` in it.
            reachability = await client.get_worker_task_reachability(
                build_ids=["2.0"],
                reachability_type=TaskReachabilityType.CLOSED_WORKFLOWS,
            )
            assert reachability.build_id_reachability["2.0"].task_queue_reachability[
                task_queue
            ] == [TaskReachabilityType.NEW_WORKFLOWS]

            await wf1.signal(WaitOnSignalWorkflow.my_signal, "finish")
            await wf2.signal(WaitOnSignalWorkflow.my_signal, "finish")
            await wf1.result()
            await wf2.result()


async def test_worker_validate_fail(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip("Java test server does not appear to fail on invalid namespace")
    # Try to run a worker on an invalid namespace
    config = client.config()
    config["namespace"] = "does-not-exist"
    client = Client(**config)
    with pytest.raises(RuntimeError) as err:
        await Worker(
            client, task_queue=f"tq-{uuid.uuid4()}", workflows=[NeverRunWorkflow]
        ).run()
    assert str(err.value).startswith("Worker validation failed")


async def test_can_run_resource_based_worker(client: Client, env: WorkflowEnvironment):
    tuner = WorkerTuner.create_resource_based(
        target_memory_usage=0.5,
        target_cpu_usage=0.5,
        workflow_config=ResourceBasedSlotConfig(5, 20, timedelta(seconds=0)),
        # Ensure we can assume defaults when specifying only some options
        activity_config=ResourceBasedSlotConfig(minimum_slots=1),
    )
    async with new_worker(
        client,
        WaitOnSignalWorkflow,
        activities=[say_hello],
        tuner=tuner,
    ) as w:
        wf1 = await client.start_workflow(
            WaitOnSignalWorkflow.run,
            id=f"resource-based-{uuid.uuid4()}",
            task_queue=w.task_queue,
        )
        await wf1.signal(WaitOnSignalWorkflow.my_signal, "finish")
        await wf1.result()


async def test_can_run_composite_tuner_worker(client: Client, env: WorkflowEnvironment):
    resource_based_options = ResourceBasedTunerConfig(0.5, 0.5)
    tuner = WorkerTuner.create_composite(
        workflow_supplier=FixedSizeSlotSupplier(5),
        activity_supplier=ResourceBasedSlotSupplier(
            ResourceBasedSlotConfig(
                minimum_slots=1,
                maximum_slots=20,
                ramp_throttle=timedelta(milliseconds=60),
            ),
            resource_based_options,
        ),
        local_activity_supplier=ResourceBasedSlotSupplier(
            ResourceBasedSlotConfig(
                minimum_slots=1,
                maximum_slots=5,
                ramp_throttle=timedelta(milliseconds=60),
            ),
            resource_based_options,
        ),
    )
    async with new_worker(
        client,
        WaitOnSignalWorkflow,
        activities=[say_hello],
        tuner=tuner,
    ) as w:
        wf1 = await client.start_workflow(
            WaitOnSignalWorkflow.run,
            id=f"composite-tuner-{uuid.uuid4()}",
            task_queue=w.task_queue,
        )
        await wf1.signal(WaitOnSignalWorkflow.my_signal, "finish")
        await wf1.result()


async def test_cant_specify_max_concurrent_and_tuner(
    client: Client, env: WorkflowEnvironment
):
    tuner = WorkerTuner.create_resource_based(
        target_memory_usage=0.5,
        target_cpu_usage=0.5,
        workflow_config=ResourceBasedSlotConfig(5, 20, timedelta(seconds=0)),
    )
    with pytest.raises(ValueError) as err:
        async with new_worker(
            client,
            WaitOnSignalWorkflow,
            activities=[say_hello],
            tuner=tuner,
            max_concurrent_workflow_tasks=10,
        ):
            pass
    assert "Cannot specify " in str(err.value)
    assert "when also specifying tuner" in str(err.value)


async def test_warns_when_workers_too_lot(client: Client, env: WorkflowEnvironment):
    tuner = WorkerTuner.create_resource_based(
        target_memory_usage=0.5,
        target_cpu_usage=0.5,
    )
    with concurrent.futures.ThreadPoolExecutor() as executor:
        with pytest.warns(
            UserWarning,
            match=f"Worker max_concurrent_activities is 500 but activity_executor's max_workers is only",
        ):
            async with new_worker(
                client,
                WaitOnSignalWorkflow,
                activities=[say_hello],
                tuner=tuner,
                activity_executor=executor,
            ):
                pass


def create_worker(
    client: Client,
    on_fatal_error: Optional[Callable[[BaseException], Awaitable[None]]] = None,
) -> Worker:
    return Worker(
        client,
        task_queue=f"task-queue-{uuid.uuid4()}",
        activities=[never_run_activity],
        workflows=[NeverRunWorkflow],
        on_fatal_error=on_fatal_error,
    )


class WorkerFailureInjector:
    def __init__(self, worker: Worker) -> None:
        self.workflow = PollFailureInjector(worker, "poll_workflow_activation")
        self.activity = PollFailureInjector(worker, "poll_activity_task")

    def __enter__(self) -> WorkerFailureInjector:
        return self

    def __exit__(self, *args, **kwargs) -> None:
        self.workflow.shutdown()
        self.activity.shutdown()


class PollFailureInjector:
    def __init__(self, worker: Worker, attr: str) -> None:
        self.worker = worker
        self.attr = attr
        self.poll_fail_queue: asyncio.Queue[Exception] = asyncio.Queue()
        self.orig_poll_call = getattr(worker._bridge_worker, attr)
        setattr(worker._bridge_worker, attr, self.patched_poll_call)
        self.next_poll_task: Optional[asyncio.Task] = None
        self.next_exception_task: Optional[asyncio.Task] = None

    async def patched_poll_call(self) -> Any:
        if not self.next_poll_task:
            self.next_poll_task = asyncio.create_task(self.orig_poll_call())
        if not self.next_exception_task:
            self.next_exception_task = asyncio.create_task(self.poll_fail_queue.get())

        await asyncio.wait(
            [self.next_poll_task, self.next_exception_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # If activation came, return that and leave queue for next poll
        if self.next_poll_task.done():
            ret = self.next_poll_task.result()
            self.next_poll_task = None
            return ret

        # Raise the error
        exc = self.next_exception_task.result()
        self.next_exception_task = None
        raise exc

    def shutdown(self) -> None:
        if self.next_poll_task:
            self.next_poll_task.cancel()
        if self.next_exception_task:
            self.next_exception_task.cancel()
        setattr(self.worker._bridge_worker, self.attr, self.orig_poll_call)
