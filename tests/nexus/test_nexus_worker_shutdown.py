"""Tests for Nexus worker shutdown support."""

import asyncio
import concurrent.futures
import gc
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import nexusrpc
import pytest
from nexusrpc.handler import (
    StartOperationContext,
    service_handler,
    sync_operation,
)

from temporalio import nexus, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import (
    make_nexus_endpoint_name,
)

# Cloud CI's namespace credentials cannot manage Nexus endpoints.
# See https://github.com/temporalio/sdk-python/issues/1704.
pytestmark = pytest.mark.requires_local_server


@nexusrpc.service
class ShutdownTestService:
    wait_for_shutdown: nexusrpc.Operation[None, str]
    hang_until_cancelled: nexusrpc.Operation[None, str]
    wait_for_shutdown_sync: nexusrpc.Operation[None, str]
    check_shutdown: nexusrpc.Operation[None, str]


@service_handler(service=ShutdownTestService)
class ShutdownTestServiceHandler:
    def __init__(
        self,
        operation_started: asyncio.Event | None = None,
        sync_operation_started: threading.Event | None = None,
    ) -> None:
        self.operation_started = operation_started
        self.sync_operation_started = sync_operation_started
        self.shutdown_check_before: bool | None = None
        self.shutdown_check_after: bool | None = None

    @sync_operation
    async def wait_for_shutdown(self, _ctx: StartOperationContext, _input: None) -> str:
        assert self.operation_started
        self.operation_started.set()
        await nexus.wait_for_worker_shutdown()
        return "Worker graceful shutdown"

    @sync_operation
    async def hang_until_cancelled(
        self, _ctx: StartOperationContext, _input: None
    ) -> str:
        assert self.operation_started
        self.operation_started.set()
        try:
            while True:
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            return "Properly cancelled"

    @sync_operation
    def wait_for_shutdown_sync(self, _ctx: StartOperationContext, _input: None) -> str:
        assert self.sync_operation_started
        self.sync_operation_started.set()
        nexus.wait_for_worker_shutdown_sync(30)
        return "Worker graceful shutdown sync"

    @sync_operation
    async def check_shutdown(self, _ctx: StartOperationContext, _input: None) -> str:
        assert self.operation_started
        self.shutdown_check_before = nexus.is_worker_shutdown()
        self.operation_started.set()
        await nexus.wait_for_worker_shutdown()
        self.shutdown_check_after = nexus.is_worker_shutdown()
        return "done"


@dataclass
class ShutdownTestCallerInput:
    operation: Literal[
        "wait_for_shutdown",
        "hang_until_cancelled",
        "wait_for_shutdown_sync",
        "check_shutdown",
    ]
    task_queue: str


@workflow.defn
class ShutdownTestCallerWorkflow:
    @workflow.run
    async def run(self, input: ShutdownTestCallerInput) -> str:
        nexus_client = workflow.create_nexus_client(
            service=ShutdownTestService,
            endpoint=make_nexus_endpoint_name(input.task_queue),
        )
        match input.operation:
            case "wait_for_shutdown":
                return await nexus_client.execute_operation(
                    ShutdownTestService.wait_for_shutdown, None
                )
            case "hang_until_cancelled":
                return await nexus_client.execute_operation(
                    ShutdownTestService.hang_until_cancelled, None
                )
            case "wait_for_shutdown_sync":
                return await nexus_client.execute_operation(
                    ShutdownTestService.wait_for_shutdown_sync, None
                )
            case "check_shutdown":
                return await nexus_client.execute_operation(
                    ShutdownTestService.check_shutdown, None
                )


@workflow.defn(sandboxed=False)
class NexusPendingOperationCallerWorkflow:
    """Caller that parks on a Nexus operation that never starts."""

    @workflow.run
    async def run(self, input: ShutdownTestCallerInput) -> str:
        nexus_client = workflow.create_nexus_client(
            service=ShutdownTestService,
            endpoint=make_nexus_endpoint_name(input.task_queue),
        )
        return await nexus_client.execute_operation(
            ShutdownTestService.hang_until_cancelled, None
        )


async def test_nexus_operation_handle_task_not_destroyed_on_shutdown(
    env: WorkflowEnvironment, caplog: pytest.LogCaptureFixture
):
    """Dropping a workflow parked on a Nexus operation must not leak a pending task.

    The Nexus operation handle creates its asyncio task directly. Before it was
    registered with the workflow instance, dropping the workflow on worker
    shutdown (or eviction) left that task pending and asyncio logged "Task was
    destroyed but it is pending!" once per waiting workflow -- unlike activity
    and child workflow handles, which register their tasks with the instance.
    """
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    caller_task_queue = str(uuid.uuid4())
    # No handler worker is started for this endpoint, so the Nexus operation
    # never starts and the caller workflow stays parked on it.
    await env.create_nexus_endpoint(
        make_nexus_endpoint_name(caller_task_queue), caller_task_queue
    )

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        async with Worker(
            env.client,
            task_queue=caller_task_queue,
            workflows=[NexusPendingOperationCallerWorkflow],
        ):
            handle = await env.client.start_workflow(
                NexusPendingOperationCallerWorkflow.run,
                ShutdownTestCallerInput(
                    operation="hang_until_cancelled", task_queue=caller_task_queue
                ),
                id=str(uuid.uuid4()),
                task_queue=caller_task_queue,
            )
            # Give the workflow time to schedule the Nexus operation before the
            # worker block exits and drops it from the cache
            await asyncio.sleep(1)

    # Give the loop a chance to GC the discarded workflow instance and its tasks
    await asyncio.sleep(0.1)
    gc.collect()
    await asyncio.sleep(0.1)

    await handle.terminate("test cleanup")

    assert not [
        r
        for r in caplog.records
        if "Task was destroyed but it is pending" in r.getMessage()
    ]


async def test_nexus_worker_shutdown(env: WorkflowEnvironment):
    """Test that Nexus operations are cancelled when worker shuts down without graceful timeout."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # Use separate task queues for caller and handler workers
    handler_task_queue = str(uuid.uuid4())
    caller_task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(
        make_nexus_endpoint_name(handler_task_queue), handler_task_queue
    )

    operation_started = asyncio.Event()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        async with Worker(
            env.client,
            task_queue=caller_task_queue,
            workflows=[ShutdownTestCallerWorkflow],
        ):
            # Handler worker (will be shut down without graceful timeout)
            handler_worker = Worker(
                env.client,
                task_queue=handler_task_queue,
                nexus_service_handlers=[ShutdownTestServiceHandler(operation_started)],
                nexus_task_executor=executor,
                # No graceful shutdown timeout - operations should be cancelled immediately
            )

            handler_worker_task = asyncio.create_task(handler_worker.run())

            # Start the operation that will hang via workflow
            handle = await env.client.start_workflow(
                ShutdownTestCallerWorkflow.run,
                ShutdownTestCallerInput(
                    operation="hang_until_cancelled",
                    task_queue=handler_task_queue,
                ),
                id=str(uuid.uuid4()),
                task_queue=caller_task_queue,
            )

            # Wait for operation to start
            await operation_started.wait()

            # Shutdown the handler worker - this should cancel the operation
            await handler_worker.shutdown()

            # The handler worker task should complete
            await handler_worker_task

            result = await handle.result()
            assert result == "Properly cancelled"


async def test_nexus_worker_shutdown_graceful(env: WorkflowEnvironment):
    """Test that async Nexus operations complete gracefully during shutdown."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # Use separate task queues for caller and handler workers
    handler_task_queue = str(uuid.uuid4())
    caller_task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(
        make_nexus_endpoint_name(handler_task_queue), handler_task_queue
    )

    operation_started = asyncio.Event()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        async with Worker(
            env.client,
            task_queue=caller_task_queue,
            workflows=[ShutdownTestCallerWorkflow],
        ):
            # Handler worker (will be shut down)
            handler_worker = Worker(
                env.client,
                task_queue=handler_task_queue,
                nexus_service_handlers=[ShutdownTestServiceHandler(operation_started)],
                nexus_task_executor=executor,
                graceful_shutdown_timeout=timedelta(seconds=5),
            )

            handler_worker_task = asyncio.create_task(handler_worker.run())

            # Start the operation that waits for shutdown via workflow
            handle = await env.client.start_workflow(
                ShutdownTestCallerWorkflow.run,
                ShutdownTestCallerInput(
                    operation="wait_for_shutdown",
                    task_queue=handler_task_queue,
                ),
                id=str(uuid.uuid4()),
                task_queue=caller_task_queue,
            )

            # Wait for operation to start
            await operation_started.wait()

            # Shutdown the handler worker - this should signal the shutdown event
            await handler_worker.shutdown()

            # The handler worker task should complete
            await handler_worker_task

            # The operation should have completed successfully
            result = await handle.result()
            assert result == "Worker graceful shutdown"


async def test_sync_nexus_operation_worker_shutdown_graceful(env: WorkflowEnvironment):
    """Test that sync (ThreadPoolExecutor) Nexus operations complete gracefully during shutdown."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # Use separate task queues for caller and handler workers
    handler_task_queue = str(uuid.uuid4())
    caller_task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(
        make_nexus_endpoint_name(handler_task_queue), handler_task_queue
    )

    sync_operation_started = threading.Event()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        async with Worker(
            env.client,
            task_queue=caller_task_queue,
            workflows=[ShutdownTestCallerWorkflow],
        ):
            # Handler worker (will be shut down)
            handler_worker = Worker(
                env.client,
                task_queue=handler_task_queue,
                nexus_service_handlers=[
                    ShutdownTestServiceHandler(
                        sync_operation_started=sync_operation_started
                    )
                ],
                nexus_task_executor=executor,
                graceful_shutdown_timeout=timedelta(seconds=5),
            )

            handler_worker_task = asyncio.create_task(handler_worker.run())

            # Start the operation that waits for shutdown synchronously via workflow
            handle = await env.client.start_workflow(
                ShutdownTestCallerWorkflow.run,
                ShutdownTestCallerInput(
                    operation="wait_for_shutdown_sync",
                    task_queue=handler_task_queue,
                ),
                id=str(uuid.uuid4()),
                task_queue=caller_task_queue,
            )

            # Wait for operation to start
            await asyncio.to_thread(sync_operation_started.wait)

            # Shutdown the handler worker - this should signal the shutdown event
            await handler_worker.shutdown()

            # The handler worker task should complete
            await handler_worker_task

            # The operation should have completed successfully
            result = await handle.result()
            assert result == "Worker graceful shutdown sync"


async def test_is_worker_shutdown(env: WorkflowEnvironment):
    """Test that is_worker_shutdown returns correct values."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # Use separate task queues for caller and handler workers
    handler_task_queue = str(uuid.uuid4())
    caller_task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(
        make_nexus_endpoint_name(handler_task_queue), handler_task_queue
    )

    operation_started = asyncio.Event()
    handler = ShutdownTestServiceHandler(operation_started)

    with concurrent.futures.ThreadPoolExecutor() as executor:
        async with Worker(
            env.client,
            task_queue=caller_task_queue,
            workflows=[ShutdownTestCallerWorkflow],
        ):
            # Handler worker (will be shut down)
            handler_worker = Worker(
                env.client,
                task_queue=handler_task_queue,
                nexus_service_handlers=[handler],
                nexus_task_executor=executor,
                graceful_shutdown_timeout=timedelta(seconds=5),
            )

            handler_worker_task = asyncio.create_task(handler_worker.run())

            # Start the operation via workflow
            handle = await env.client.start_workflow(
                ShutdownTestCallerWorkflow.run,
                ShutdownTestCallerInput(
                    operation="check_shutdown",
                    task_queue=handler_task_queue,
                ),
                id=str(uuid.uuid4()),
                task_queue=caller_task_queue,
            )

            # Wait for operation to start
            await operation_started.wait()

            # Shutdown the handler worker
            await handler_worker.shutdown()

            await handler_worker_task
            result = await handle.result()

            assert handler.shutdown_check_before is False
            assert handler.shutdown_check_after is True
            assert result == "done"
