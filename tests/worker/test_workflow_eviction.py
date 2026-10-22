import asyncio
import threading
import uuid
from collections.abc import Sequence

import pytest

import temporalio.converter
import temporalio.converter._extstore
import temporalio.workflow
from temporalio.bridge.proto.workflow_activation import WorkflowActivation
from temporalio.bridge.proto.workflow_completion import WorkflowActivationCompletion
from temporalio.client import Client
from temporalio.worker import WorkflowInstance, WorkflowInstanceDetails, WorkflowRunner
from temporalio.worker._command_aware_visitor import CommandInfo
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from tests.helpers import assert_eventually, assert_task_fail_eventually, new_worker


@temporalio.workflow.defn
class EvictionCompletionWorkflow:
    @temporalio.workflow.run
    async def run(self) -> str:
        return "done"


class _BlockingRunner(WorkflowRunner):
    def __init__(self, *, block_eviction: bool) -> None:
        self._runner = SandboxedWorkflowRunner()
        self.block_eviction = block_eviction
        self.started = threading.Event()
        self.release = threading.Event()
        self.instances: list[_BlockingInstance] = []

    def prepare_workflow(self, defn: temporalio.workflow._Definition) -> None:
        self._runner.prepare_workflow(defn)

    def create_instance(self, det: WorkflowInstanceDetails) -> WorkflowInstance:
        instance = _BlockingInstance(self, self._runner.create_instance(det))
        self.instances.append(instance)
        return instance

    def set_worker_level_failure_exception_types(
        self, types: Sequence[type[BaseException]]
    ) -> None:
        self._runner.set_worker_level_failure_exception_types(types)


class _BlockingInstance(WorkflowInstance):
    def __init__(self, runner: _BlockingRunner, instance: WorkflowInstance) -> None:
        self._runner = runner
        self._instance = instance
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.eviction_calls = 0

    def activate(self, act: WorkflowActivation) -> WorkflowActivationCompletion:
        evicting = any(job.HasField("remove_from_cache") for job in act.jobs)
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if evicting:
                self.eviction_calls += 1
            should_block = (
                evicting == self._runner.block_eviction
                and not self._runner.started.is_set()
            )
            if should_block:
                self._runner.started.set()
        try:
            # Hold the executor thread outside the sandbox so the deadlock
            # interruption cannot complete it before the test releases it.
            if should_block:
                assert self._runner.release.wait(timeout=15)
            return self._instance.activate(act)
        finally:
            with self._lock:
                self._active -= 1

    def get_serialization_context(
        self, command_info: CommandInfo | None
    ) -> temporalio.converter.SerializationContext | None:
        return self._instance.get_serialization_context(command_info)

    def get_external_store_context(
        self, command_info: CommandInfo | None
    ) -> temporalio.converter._extstore.StorageDriverStoreContext:
        return self._instance.get_external_store_context(command_info)

    def get_info(self) -> temporalio.workflow.Info:
        return self._instance.get_info()

    def get_thread_id(self) -> int | None:
        return self._instance.get_thread_id()


async def test_workflow_eviction_waits_for_timed_out_activation(client: Client) -> None:
    runner = _BlockingRunner(block_eviction=False)
    async with new_worker(
        client,
        EvictionCompletionWorkflow,
        workflow_runner=runner,
        max_concurrent_workflow_tasks=2,
    ) as worker:
        workflow_worker = worker._workflow_worker
        assert workflow_worker is not None
        workflow_worker._deadlock_timeout_seconds = 1
        try:
            handle = await client.start_workflow(
                EvictionCompletionWorkflow.run,
                id=f"workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
            )
            await assert_task_fail_eventually(handle, message_contains="deadlock")
            assert runner.started.is_set()
            instance = runner.instances[0]
            assert instance.eviction_calls == 0
            assert handle.result_run_id is not None
            running = workflow_worker._running_workflows[handle.result_run_id]
            assert isinstance(running.deadlocked_activation_task, asyncio.Future)
            assert not running.deadlocked_activation_task.done()
            runner.release.set()
            assert await asyncio.wait_for(handle.result(), timeout=10) == "done"

            async def check_evicted() -> None:
                assert instance.eviction_calls == 1

            await assert_eventually(check_evicted)
            assert instance.max_active == 1
        finally:
            runner.release.set()


@pytest.mark.parametrize("hold_across_retry", [False, True])
async def test_workflow_eviction_waits_for_cleanup_completion(
    client: Client, hold_across_retry: bool
) -> None:
    runner = _BlockingRunner(block_eviction=True)
    async with new_worker(
        client,
        EvictionCompletionWorkflow,
        workflow_runner=runner,
        max_concurrent_workflow_tasks=2,
    ) as worker:
        workflow_worker = worker._workflow_worker
        assert workflow_worker is not None
        workflow_worker._deadlock_timeout_seconds = 1
        evicted = asyncio.Event()
        workflow_worker._on_eviction_hook = lambda _run_id, _job: evicted.set()
        try:
            assert (
                await client.execute_workflow(
                    EvictionCompletionWorkflow.run,
                    id=f"workflow-{uuid.uuid4()}",
                    task_queue=worker.task_queue,
                )
                == "done"
            )

            async def check_timed_out() -> None:
                assert workflow_worker._could_not_evict_count == 1

            await assert_eventually(check_timed_out)
            if hold_across_retry:
                # The retry delay is two seconds. Keeping the same execution
                # blocked past it detects an overlapping eviction submission.
                await asyncio.sleep(2.4)
            assert not evicted.is_set()
            runner.release.set()
            await asyncio.wait_for(evicted.wait(), timeout=10)
            assert runner.instances[0].eviction_calls == 1
            assert runner.instances[0].max_active == 1
            assert workflow_worker._could_not_evict_count == 0
        finally:
            runner.release.set()
