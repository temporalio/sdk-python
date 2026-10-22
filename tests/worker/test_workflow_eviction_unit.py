from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, Mock

import pytest

import temporalio.bridge.worker
from temporalio.bridge.proto.workflow_activation import (
    RemoveFromCache,
    WorkflowActivation,
)
from temporalio.bridge.proto.workflow_completion import WorkflowActivationCompletion
from temporalio.common import MetricMeter
from temporalio.converter import DataConverter
from temporalio.worker._workflow import _RunningWorkflow, _WorkflowWorker
from temporalio.worker._workflow_instance import (
    UnsandboxedWorkflowRunner,
    WorkflowInstance,
)
from tests.helpers import assert_eventually


class EvictionHarness:
    def __init__(self) -> None:
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.release = threading.Event()
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.calls = 0
        self.instance = Mock(spec=WorkflowInstance)
        self.instance.activate.side_effect = self.activate
        self.workflow = _RunningWorkflow(self.instance, "workflow-id")
        self.bridge = Mock(spec=temporalio.bridge.worker.Worker)
        self.bridge.complete_workflow_activation = AsyncMock()
        self.worker = _WorkflowWorker(
            bridge_worker=lambda: self.bridge,
            namespace="default",
            task_queue="test-eviction",
            workflows=[],
            workflow_task_executor=self.executor,
            max_concurrent_workflow_tasks=2,
            workflow_runner=UnsandboxedWorkflowRunner(),
            unsandboxed_workflow_runner=UnsandboxedWorkflowRunner(),
            data_converter=DataConverter.default,
            interceptors=[],
            workflow_failure_exception_types=[],
            patch_activation_callback=None,
            debug_mode=False,
            disable_eager_activity_execution=False,
            metric_meter=MetricMeter.noop,
            on_eviction_hook=None,
            disable_safe_eviction=False,
            should_enforce_versioning_behavior=False,
            assert_local_activity_valid=lambda _: None,
            encode_headers=False,
            max_workflow_task_external_storage_concurrency=1,
        )
        self.worker._deadlock_timeout_seconds = 1
        self.worker._running_workflows["run-id"] = self.workflow
        self.job = RemoveFromCache(message="test")
        self.activation = WorkflowActivation(run_id="run-id")
        self.activation.jobs.add().remove_from_cache.CopyFrom(self.job)
        self.tasks: list[asyncio.Task[None]] = []

    def activate(self, act: WorkflowActivation) -> WorkflowActivationCompletion:
        self.calls += 1
        self.loop.call_soon_threadsafe(self.started.set)
        self.release.wait()
        self.loop.call_soon_threadsafe(self.finished.set)
        return WorkflowActivationCompletion(run_id=act.run_id)

    def evict(self) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self.worker._handle_cache_eviction(self.activation, self.job)
        )
        self.tasks.append(task)
        return task

    async def wait_for_timeout(self) -> None:
        async def check() -> None:
            assert self.worker._could_not_evict_count == 1

        await assert_eventually(check)

    def assert_not_evicted(self) -> None:
        assert self.worker._running_workflows["run-id"] is self.workflow
        self.bridge.complete_workflow_activation.assert_not_awaited()


@pytest.fixture
async def eviction() -> AsyncIterator[EvictionHarness]:
    harness = EvictionHarness()
    try:
        yield harness
    finally:
        harness.release.set()
        await asyncio.gather(*harness.tasks, return_exceptions=True)
        harness.executor.shutdown()


async def test_eviction_consumes_success_during_retry_delay(
    eviction: EvictionHarness,
) -> None:
    task = eviction.evict()
    await eviction.wait_for_timeout()
    eviction.assert_not_evicted()
    eviction.release.set()
    await eviction.finished.wait()
    await task
    assert eviction.calls == 1
    assert "run-id" not in eviction.worker._running_workflows
    eviction.bridge.complete_workflow_activation.assert_awaited_once()
    assert eviction.worker._could_not_evict_count == 0


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("activation failed"),
        asyncio.TimeoutError(),
        asyncio.CancelledError(),
    ],
)
async def test_eviction_retries_completed_failure(
    eviction: EvictionHarness, error: BaseException
) -> None:
    eviction.instance.activate.side_effect = [
        error,
        WorkflowActivationCompletion(run_id="run-id"),
    ]
    await eviction.evict()
    assert eviction.instance.activate.call_count == 2
    eviction.bridge.complete_workflow_activation.assert_awaited_once()
    assert eviction.worker._could_not_evict_count == 0


async def test_eviction_cancellation_waiting_for_deadlocked_activation(
    eviction: EvictionHarness,
) -> None:
    activation = eviction.loop.run_in_executor(
        eviction.executor, eviction.workflow.activate, eviction.activation
    )
    eviction.workflow.deadlocked_activation_task = activation
    await eviction.started.wait()
    task = eviction.evict()
    await asyncio.sleep(0)
    task.cancel()
    # A cancelled handler must not turn the still-running thread into a completed
    # activation and start cleanup on the same workflow instance.
    eviction.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not activation.cancelled()
    await activation
    assert eviction.calls == 1
    eviction.assert_not_evicted()


async def test_eviction_cancellation_waiting_for_cleanup(
    eviction: EvictionHarness,
) -> None:
    task = eviction.evict()
    await eviction.started.wait()
    task.cancel()
    eviction.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert eviction.calls == 1
    eviction.assert_not_evicted()


async def test_activation_cancellation_preserves_completion_for_eviction(
    eviction: EvictionHarness,
) -> None:
    eviction.instance.get_info.return_value.workflow_type = "TestWorkflow"
    task = asyncio.create_task(
        eviction.worker._handle_activation(WorkflowActivation(run_id="run-id"))
    )
    eviction.tasks.append(task)
    await eviction.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    activation = eviction.workflow.deadlocked_activation_task
    assert activation is not None
    assert not activation.done()
    eviction.assert_not_evicted()
    eviction.release.set()
    await activation
    await eviction.evict()
    assert eviction.calls == 2
    eviction.bridge.complete_workflow_activation.assert_awaited_once()


async def test_activation_execution_timeout_is_not_a_deadlock(
    eviction: EvictionHarness,
) -> None:
    eviction.instance.get_info.return_value.workflow_type = "TestWorkflow"
    eviction.instance.activate.side_effect = asyncio.TimeoutError("execution timeout")
    await eviction.worker._handle_activation(WorkflowActivation(run_id="run-id"))
    assert eviction.workflow.deadlocked_activation_task is None
    eviction.bridge.complete_workflow_activation.assert_awaited_once()
    completion = eviction.bridge.complete_workflow_activation.call_args.args[0]
    assert completion.failed.failure.message == "execution timeout"
