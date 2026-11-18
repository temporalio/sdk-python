from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
from collections.abc import Callable, Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import temporalio.bridge.proto.workflow_activation as workflow_activation
import temporalio.bridge.proto.workflow_completion as workflow_completion
import temporalio.bridge.worker
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.worker
import temporalio.worker._workflow
import temporalio.worker._workflow_instance
import temporalio.worker.workflow_sandbox
import temporalio.workflow
from tests.helpers import new_worker


@pytest.fixture
def workflow_worker() -> Iterator[temporalio.worker._workflow._WorkflowWorker]:
    bridge = Mock(spec=temporalio.bridge.worker.Worker)
    bridge.complete_workflow_activation = AsyncMock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        yield temporalio.worker._workflow._WorkflowWorker(
            bridge_worker=lambda: bridge,
            namespace="default",
            task_queue="test-deadlock",
            workflows=[],
            workflow_task_executor=executor,
            max_concurrent_workflow_tasks=None,
            workflow_runner=temporalio.worker.UnsandboxedWorkflowRunner(),
            unsandboxed_workflow_runner=temporalio.worker.UnsandboxedWorkflowRunner(),
            data_converter=temporalio.converter.DataConverter.default,
            interceptors=[],
            workflow_failure_exception_types=[],
            patch_activation_callback=None,
            debug_mode=False,
            disable_eager_activity_execution=False,
            metric_meter=temporalio.common.MetricMeter.noop,
            on_eviction_hook=None,
            disable_safe_eviction=False,
            should_enforce_versioning_behavior=False,
            assert_local_activity_valid=lambda _: None,
            encode_headers=False,
            max_workflow_task_external_storage_concurrency=3,
        )


def eviction_activation() -> workflow_activation.WorkflowActivation:
    return workflow_activation.WorkflowActivation(
        run_id="run",
        jobs=[
            workflow_activation.WorkflowActivationJob(
                remove_from_cache=workflow_activation.RemoveFromCache()
            )
        ],
    )


@pytest.mark.parametrize(
    "activation_error",
    [None, RuntimeError, temporalio.worker._workflow._InterruptDeadlockError],
)
async def test_workflow_deadlock_waits_for_activation(
    workflow_worker: temporalio.worker._workflow._WorkflowWorker,
    monkeypatch: pytest.MonkeyPatch,
    activation_error: type[BaseException] | None,
):
    release = threading.Event()
    activation_finished = threading.Event()
    eviction_overlapped: list[bool] = []

    def activate(
        act: workflow_activation.WorkflowActivation,
    ) -> workflow_completion.WorkflowActivationCompletion:
        if act.jobs:
            eviction_overlapped.append(not activation_finished.is_set())
        else:
            try:
                assert release.wait(15)
                if activation_error:
                    raise activation_error()
            finally:
                activation_finished.set()
        return workflow_completion.WorkflowActivationCompletion()

    instance = Mock(spec=temporalio.worker.WorkflowInstance)
    instance.activate.side_effect = activate
    instance.get_thread_id.return_value = None
    instance.get_info.return_value = Mock(workflow_type="test")
    running = temporalio.worker._workflow._RunningWorkflow(instance, "workflow")
    interrupt = Mock()
    monkeypatch.setattr(running, "attempt_deadlock_interruption", interrupt)
    workflow_worker._running_workflows["run"] = running
    workflow_worker._deadlock_timeout_seconds = 1
    eviction_task: asyncio.Task[None] | None = None
    try:
        await workflow_worker._handle_activation(
            workflow_activation.WorkflowActivation(run_id="run")
        )

        bridge = cast(Mock, workflow_worker._bridge_worker())
        completion = bridge.complete_workflow_activation.call_args.args[0]
        assert "TMPRL1101" in completion.failed.failure.message
        interrupt.assert_called_once_with()
        activation_future = cast(asyncio.Future, running.deadlocked_activation_task)
        assert not activation_future.done()

        eviction_task = asyncio.create_task(
            workflow_worker._handle_activation(eviction_activation())
        )
        await asyncio.sleep(0)
        assert not eviction_task.done()
        assert not eviction_overlapped
    finally:
        release.set()
        if eviction_task:
            await asyncio.wait_for(eviction_task, 10)
        elif running.deadlocked_activation_task:
            await asyncio.gather(
                running.deadlocked_activation_task, return_exceptions=True
            )

    assert eviction_overlapped == [False]
    assert "run" not in workflow_worker._running_workflows
    assert bridge.complete_workflow_activation.call_count == 2


async def test_workflow_deadlock_cancels_queued_activation(
    workflow_worker: temporalio.worker._workflow._WorkflowWorker,
    monkeypatch: pytest.MonkeyPatch,
):
    release = threading.Event()
    executor = workflow_worker._workflow_task_executor
    instance = Mock(spec=temporalio.worker.WorkflowInstance)
    instance.get_info.return_value = Mock(workflow_type="test")
    instance.activate.return_value = workflow_completion.WorkflowActivationCompletion()
    running = temporalio.worker._workflow._RunningWorkflow(instance, "workflow")
    interrupt = Mock()
    monkeypatch.setattr(running, "attempt_deadlock_interruption", interrupt)
    workflow_worker._running_workflows["run"] = running
    workflow_worker._deadlock_timeout_seconds = 1
    try:
        # Fill both executor threads before submitting the activation.
        for _ in range(2):
            executor.submit(release.wait, 15)
        await workflow_worker._handle_activation(
            workflow_activation.WorkflowActivation(run_id="run")
        )
        bridge = cast(Mock, workflow_worker._bridge_worker())
        completion = bridge.complete_workflow_activation.call_args.args[0]
        assert "TMPRL1101" in completion.failed.failure.message
        assert running.deadlocked_activation_task is None
        interrupt.assert_not_called()
        instance.activate.assert_not_called()
    finally:
        release.set()
        if running.deadlocked_activation_task is not None:
            await asyncio.gather(
                running.deadlocked_activation_task, return_exceptions=True
            )

    evict = eviction_activation()
    await workflow_worker._handle_activation(evict)
    # Once executor capacity is available, only the eviction should run.
    instance.activate.assert_called_once_with(evict)
    assert "run" not in workflow_worker._running_workflows
    assert bridge.complete_workflow_activation.call_count == 2


@pytest.mark.parametrize("fail_first", [False, True])
@pytest.mark.parametrize("finish_during_backoff", [False, True])
async def test_workflow_eviction_timeout_reuses_activation(
    workflow_worker: temporalio.worker._workflow._WorkflowWorker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fail_first: bool,
    finish_during_backoff: bool,
):
    release = threading.Event()
    calls = 0

    def activate(
        _act: workflow_activation.WorkflowActivation,
    ) -> workflow_completion.WorkflowActivationCompletion:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert release.wait(15)
            if fail_first:
                raise RuntimeError("retry this eviction")
        return workflow_completion.WorkflowActivationCompletion()

    instance = Mock(spec=temporalio.worker.WorkflowInstance)
    instance.activate.side_effect = activate
    running = temporalio.worker._workflow._RunningWorkflow(instance, "workflow")
    workflow_worker._running_workflows["run"] = running
    workflow_worker._deadlock_timeout_seconds = 1
    loop = asyncio.get_running_loop()
    run_in_executor = loop.run_in_executor
    eviction_futures: list[asyncio.Future] = []

    def record_eviction(
        executor: concurrent.futures.Executor | None,
        func: Callable[..., Any],
        *args: Any,
    ) -> asyncio.Future:
        future = run_in_executor(executor, func, *args)
        if asyncio.current_task() is eviction_task:
            eviction_futures.append(future)
        return future

    backoff_started = asyncio.Event()
    retry = asyncio.Event()
    backoff_finished = asyncio.Event()
    sleep = asyncio.sleep

    async def gate_backoff(delay: float, result: Any = None) -> Any:
        if asyncio.current_task() is not eviction_task:
            return await sleep(delay, result)
        if not backoff_started.is_set():
            backoff_started.set()
            await retry.wait()
            backoff_finished.set()
        return result

    monkeypatch.setattr(loop, "run_in_executor", record_eviction)
    monkeypatch.setattr(asyncio, "sleep", gate_backoff)
    bridge = cast(Mock, workflow_worker._bridge_worker())
    eviction_task = asyncio.create_task(
        workflow_worker._handle_activation(eviction_activation())
    )
    try:
        await asyncio.wait_for(backoff_started.wait(), 5)
        assert "Timed out running eviction job" in caplog.text
        assert len(eviction_futures) == 1
        first_eviction = eviction_futures[0]
        assert not first_eviction.done()
        assert workflow_worker._could_not_evict_count == 1
        bridge.complete_workflow_activation.assert_not_called()
        if finish_during_backoff:
            # Wait for completion without consuming its result or exception.
            release.set()
            done, _ = await asyncio.wait((first_eviction,), timeout=5)
            assert first_eviction in done
        else:
            # Let the retry loop reach its next wait before releasing the worker
            # thread. It must wait on the existing call, not submit another.
            retry.set()
            await asyncio.wait_for(backoff_finished.wait(), 5)
            assert len(eviction_futures) == 1
            assert not eviction_task.done()
    finally:
        release.set()
        retry.set()
        await asyncio.wait_for(eviction_task, 10)

    assert len(eviction_futures) == (2 if fail_first else 1)
    assert workflow_worker._could_not_evict_count == 0
    assert "run" not in workflow_worker._running_workflows
    bridge.complete_workflow_activation.assert_called_once()
    assert bridge.complete_workflow_activation.call_args.args[0].HasField("successful")


@temporalio.workflow.defn
class DeadlockEvictionWorkflow:
    def __init__(self) -> None:
        self.poked = False

    @temporalio.workflow.run
    async def run(self) -> None:
        await temporalio.workflow.wait_condition(lambda: self.poked)

    @temporalio.workflow.signal
    def poke(self) -> None:
        self.poked = True

    @temporalio.workflow.query
    def state(self) -> bool:
        return self.poked


@pytest.mark.parametrize("sandboxed", [False, True])
async def test_workflow_eviction_after_lost_task_done_callback(
    client: temporalio.client.Client,
    monkeypatch: pytest.MonkeyPatch,
    sandboxed: bool,
):
    impl = temporalio.worker._workflow_instance._WorkflowInstanceImpl
    original_call_soon = impl.call_soon
    armed = False
    interrupted: list[
        tuple[temporalio.worker._workflow_instance._WorkflowInstanceImpl, asyncio.Task]
    ] = []

    def call_soon(
        self: temporalio.worker._workflow_instance._WorkflowInstanceImpl,
        callback: Callable[..., object],
        *args: Any,
        context: contextvars.Context | None = None,
    ) -> asyncio.Handle:
        nonlocal armed
        task = args[0] if args else None
        if (
            armed
            and getattr(callback, "__self__", None) is self._tasks
            and isinstance(task, asyncio.Task)
            and task.get_name().startswith("query:")
        ):
            armed = False
            interrupted.append((self, task))
            # Reproduce #2067 without an actual asynchronous thread interrupt,
            # so a regression cannot corrupt the test worker's event loop.
            raise temporalio.worker._workflow._InterruptDeadlockError()
        return original_call_soon(self, callback, *args, context=context)

    monkeypatch.setattr(impl, "call_soon", call_soon)
    worker = new_worker(
        client,
        DeadlockEvictionWorkflow,
        workflow_runner=(
            temporalio.worker.workflow_sandbox.SandboxedWorkflowRunner()
            if sandboxed
            else temporalio.worker.UnsandboxedWorkflowRunner()
        ),
    )
    evicted = asyncio.Event()
    assert worker._workflow_worker
    worker._workflow_worker._on_eviction_hook = lambda _run_id, _job: evicted.set()
    async with worker:
        try:
            handle = await client.start_workflow(
                DeadlockEvictionWorkflow.run,
                id=f"workflow-{worker.task_queue}",
                task_queue=worker.task_queue,
            )
            assert not await handle.query(DeadlockEvictionWorkflow.state)
            armed = True
            assert not await handle.query(DeadlockEvictionWorkflow.state)
            assert len(interrupted) == 1
            instance, interrupted_task = interrupted[0]
            assert interrupted_task.done()
            assert interrupted_task in instance._tasks

            assert handle.result_run_id
            worker._bridge_worker.request_workflow_eviction(handle.result_run_id)
            await asyncio.wait_for(evicted.wait(), 10)
            assert not instance._tasks
            await handle.signal(DeadlockEvictionWorkflow.poke)
            await asyncio.wait_for(handle.result(), 10)
        finally:
            # Keep shutdown safe even when the finished-task invariant regresses.
            for instance, interrupted_task in interrupted:
                instance._tasks.discard(interrupted_task)
