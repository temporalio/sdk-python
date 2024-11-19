from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

import pytest

import temporalio.worker._worker
from temporalio import activity, workflow
from temporalio.client import BuildIdOpAddNewDefault, Client, TaskReachabilityType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    ActivitySlotInfo,
    CustomSlotSupplier,
    FixedSizeSlotSupplier,
    LocalActivitySlotInfo,
    ResourceBasedSlotConfig,
    ResourceBasedSlotSupplier,
    ResourceBasedTunerConfig,
    SlotMarkUsedContext,
    SlotPermit,
    SlotReleaseContext,
    SlotReserveContext,
    Worker,
    WorkerTuner,
    WorkflowSlotInfo,
)
from temporalio.workflow import VersioningIntent
from tests.helpers import assert_eq_eventually, new_worker, worker_versioning_enabled


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
            match="Worker max_concurrent_activities is 500 but activity_executor's max_workers is only",
        ):
            async with new_worker(
                client,
                WaitOnSignalWorkflow,
                activities=[say_hello],
                tuner=tuner,
                activity_executor=executor,
            ):
                pass


async def test_custom_slot_supplier(client: Client, env: WorkflowEnvironment):
    class MyPermit(SlotPermit):
        def __init__(self, pnum: int):
            super().__init__()
            self.pnum = pnum

    class MySlotSupplier(CustomSlotSupplier):
        reserves = 0
        releases = 0
        highest_seen_reserve_on_release = 0
        used = 0
        seen_sticky_kinds = set()
        seen_slot_kinds = set()
        seen_used_slot_kinds = set()
        seen_release_info_empty = False
        seen_release_info_nonempty = False

        async def reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit:
            self.reserve_asserts(ctx)
            # Verify an async call doesn't bungle things
            await asyncio.sleep(0.01)
            self.reserves += 1
            return MyPermit(self.reserves)

        def try_reserve_slot(self, ctx: SlotReserveContext) -> Optional[SlotPermit]:
            self.reserve_asserts(ctx)
            return None

        def mark_slot_used(self, ctx: SlotMarkUsedContext) -> None:
            assert ctx.permit is not None
            assert isinstance(ctx.permit, MyPermit)
            assert ctx.permit.pnum is not None
            assert ctx.slot_info is not None
            if isinstance(ctx.slot_info, WorkflowSlotInfo):
                self.seen_used_slot_kinds.add("wf")
            elif isinstance(ctx.slot_info, ActivitySlotInfo):
                self.seen_used_slot_kinds.add("a")
            elif isinstance(ctx.slot_info, LocalActivitySlotInfo):
                self.seen_used_slot_kinds.add("la")
            self.used += 1

        def release_slot(self, ctx: SlotReleaseContext) -> None:
            assert ctx.permit is not None
            assert isinstance(ctx.permit, MyPermit)
            assert ctx.permit.pnum is not None
            self.highest_seen_reserve_on_release = max(
                ctx.permit.pnum, self.highest_seen_reserve_on_release
            )
            # Info may be empty, and we should see both empty and not
            if ctx.slot_info is None:
                self.seen_release_info_empty = True
            else:
                self.seen_release_info_nonempty = True
            self.releases += 1

        def reserve_asserts(self, ctx):
            assert ctx.task_queue is not None
            assert ctx.worker_identity is not None
            assert ctx.worker_build_id is not None
            self.seen_sticky_kinds.add(ctx.is_sticky)
            self.seen_slot_kinds.add(ctx.slot_type)

    ss = MySlotSupplier()

    tuner = WorkerTuner.create_composite(
        workflow_supplier=ss, activity_supplier=ss, local_activity_supplier=ss
    )
    async with new_worker(
        client,
        WaitOnSignalWorkflow,
        activities=[say_hello],
        tuner=tuner,
        identity="myworker",
    ) as w:
        wf1 = await client.start_workflow(
            WaitOnSignalWorkflow.run,
            id=f"custom-slot-supplier-{uuid.uuid4()}",
            task_queue=w.task_queue,
        )
        await wf1.signal(WaitOnSignalWorkflow.my_signal, "finish")
        await wf1.result()

    # We can't use reserve number directly because there is a technically possible race
    # where the python reserve function appears to complete, but Rust doesn't see that.
    # This isn't solvable without redoing a chunk of pyo3-asyncio. So we only check
    # that the permits passed to release line up.
    assert ss.highest_seen_reserve_on_release == ss.releases
    # Two workflow tasks, one activity
    assert ss.used == 3
    assert ss.seen_sticky_kinds == {True, False}
    assert ss.seen_slot_kinds == {"workflow", "activity", "local-activity"}
    assert ss.seen_used_slot_kinds == {"wf", "a"}
    assert ss.seen_release_info_empty
    assert ss.seen_release_info_nonempty


@workflow.defn
class SimpleWorkflow:
    @workflow.run
    async def run(self) -> str:
        return "hi"


async def test_throwing_slot_supplier(client: Client, env: WorkflowEnvironment):
    """Ensures a (mostly) broken slot supplier doesn't hose everything up"""

    class ThrowingSlotSupplier(CustomSlotSupplier):
        marked_used = False

        async def reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit:
            # Hand out workflow tasks until one is used
            if ctx.slot_type == "workflow" and not self.marked_used:
                return SlotPermit()
            raise ValueError("I always throw")

        def try_reserve_slot(self, ctx: SlotReserveContext) -> Optional[SlotPermit]:
            raise ValueError("I always throw")

        def mark_slot_used(self, ctx: SlotMarkUsedContext) -> None:
            raise ValueError("I always throw")

        def release_slot(self, ctx: SlotReleaseContext) -> None:
            raise ValueError("I always throw")

    ss = ThrowingSlotSupplier()

    tuner = WorkerTuner.create_composite(
        workflow_supplier=ss, activity_supplier=ss, local_activity_supplier=ss
    )
    async with new_worker(
        client,
        SimpleWorkflow,
        activities=[say_hello],
        tuner=tuner,
    ) as w:
        wf1 = await client.start_workflow(
            SimpleWorkflow.run,
            id=f"throwing-slot-supplier-{uuid.uuid4()}",
            task_queue=w.task_queue,
        )
        await wf1.result()


async def test_blocking_slot_supplier(client: Client, env: WorkflowEnvironment):
    class BlockingSlotSupplier(CustomSlotSupplier):
        marked_used = False

        async def reserve_slot(self, ctx: SlotReserveContext) -> SlotPermit:
            await asyncio.get_event_loop().create_future()
            raise ValueError("Should be unreachable")

        def try_reserve_slot(self, ctx: SlotReserveContext) -> Optional[SlotPermit]:
            return None

        def mark_slot_used(self, ctx: SlotMarkUsedContext) -> None:
            return None

        def release_slot(self, ctx: SlotReleaseContext) -> None:
            return None

    ss = BlockingSlotSupplier()

    tuner = WorkerTuner.create_composite(
        workflow_supplier=ss, activity_supplier=ss, local_activity_supplier=ss
    )
    async with new_worker(
        client,
        SimpleWorkflow,
        activities=[say_hello],
        tuner=tuner,
    ) as _w:
        await asyncio.sleep(1)


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
