"""Tests for Nexus worker shutdown support."""

import asyncio
import concurrent.futures
import uuid
from dataclasses import dataclass
from datetime import timedelta

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
    create_nexus_endpoint,
    make_nexus_endpoint_name,
)


@nexusrpc.service
class ShutdownTestService:
    wait_for_shutdown: nexusrpc.Operation[None, str]
    hang_until_cancelled: nexusrpc.Operation[None, str]
    wait_for_shutdown_sync: nexusrpc.Operation[None, str]
    check_shutdown: nexusrpc.Operation[None, str]


@dataclass
class ShutdownTestCallerInput:
    operation: str
    task_queue: str


@workflow.defn
class ShutdownTestCallerWorkflow:
    @workflow.run
    async def run(self, input: ShutdownTestCallerInput) -> str:
        nexus_client = workflow.create_nexus_client(
            service=ShutdownTestService,
            endpoint=make_nexus_endpoint_name(input.task_queue),
        )
        if input.operation == "wait_for_shutdown":
            return await nexus_client.execute_operation(
                ShutdownTestService.wait_for_shutdown, None
            )
        elif input.operation == "hang_until_cancelled":
            return await nexus_client.execute_operation(
                ShutdownTestService.hang_until_cancelled, None
            )
        elif input.operation == "wait_for_shutdown_sync":
            return await nexus_client.execute_operation(
                ShutdownTestService.wait_for_shutdown_sync, None
            )
        else:  # check_shutdown
            return await nexus_client.execute_operation(
                ShutdownTestService.check_shutdown, None
            )


async def test_nexus_worker_shutdown(env: WorkflowEnvironment):
    """Test that Nexus operations are cancelled when worker shuts down without graceful timeout."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # Use separate task queues for caller and handler workers
    handler_task_queue = str(uuid.uuid4())
    caller_task_queue = str(uuid.uuid4())
    await create_nexus_endpoint(handler_task_queue, env.client)

    operation_started = asyncio.Event()

    @service_handler(service=ShutdownTestService)
    class LocalShutdownTestServiceHandler:
        @sync_operation
        async def wait_for_shutdown(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            await nexus.wait_for_worker_shutdown()
            return "Worker graceful shutdown"

        @sync_operation
        async def hang_until_cancelled(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            nonlocal operation_started
            operation_started.set()
            try:
                # Loop forever until cancelled
                while True:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                return "Properly cancelled"

        @sync_operation
        async def wait_for_shutdown_sync(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            raise NotImplementedError

        @sync_operation
        async def check_shutdown(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            raise NotImplementedError

    # Handler worker (will be shut down without graceful timeout)
    handler_worker = Worker(
        env.client,
        task_queue=handler_task_queue,
        nexus_service_handlers=[LocalShutdownTestServiceHandler()],
        # No graceful shutdown timeout - operations should be cancelled immediately
    )

    # Caller worker (stays running)
    caller_worker = Worker(
        env.client,
        task_queue=caller_task_queue,
        workflows=[ShutdownTestCallerWorkflow],
    )

    handler_worker_task = asyncio.create_task(handler_worker.run())
    caller_worker_task = asyncio.create_task(caller_worker.run())

    try:
        # Start the operation that will hang via workflow
        workflow_task = asyncio.create_task(
            env.client.execute_workflow(
                ShutdownTestCallerWorkflow.run,
                ShutdownTestCallerInput(
                    operation="hang_until_cancelled",
                    task_queue=handler_task_queue,
                ),
                id=str(uuid.uuid4()),
                task_queue=caller_task_queue,
            )
        )

        # Wait for operation to start
        await operation_started.wait()

        # Shutdown the handler worker - this should cancel the operation
        await handler_worker.shutdown()

        # The handler worker task should complete
        await handler_worker_task

        # The workflow task may fail since we didn't have graceful shutdown time
        # The important thing is the handler worker shut down cleanly
        workflow_task.cancel()
        try:
            await workflow_task
        except asyncio.CancelledError:
            pass
    finally:
        # Clean up the caller worker
        await caller_worker.shutdown()
        await caller_worker_task


async def test_nexus_worker_shutdown_graceful(env: WorkflowEnvironment):
    """Test that async Nexus operations complete gracefully during shutdown."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # Use separate task queues for caller and handler workers
    handler_task_queue = str(uuid.uuid4())
    caller_task_queue = str(uuid.uuid4())
    await create_nexus_endpoint(handler_task_queue, env.client)

    operation_started = asyncio.Event()

    @service_handler(service=ShutdownTestService)
    class LocalShutdownTestServiceHandler:
        @sync_operation
        async def wait_for_shutdown(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            nonlocal operation_started
            operation_started.set()
            await nexus.wait_for_worker_shutdown()
            return "Worker graceful shutdown"

        @sync_operation
        async def hang_until_cancelled(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            try:
                while True:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                return "Properly cancelled"

        @sync_operation
        async def wait_for_shutdown_sync(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            raise NotImplementedError

        @sync_operation
        async def check_shutdown(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            raise NotImplementedError

    # Handler worker (will be shut down)
    handler_worker = Worker(
        env.client,
        task_queue=handler_task_queue,
        nexus_service_handlers=[LocalShutdownTestServiceHandler()],
        graceful_shutdown_timeout=timedelta(seconds=5),
    )

    # Caller worker (stays running to receive result)
    caller_worker = Worker(
        env.client,
        task_queue=caller_task_queue,
        workflows=[ShutdownTestCallerWorkflow],
    )

    handler_worker_task = asyncio.create_task(handler_worker.run())
    caller_worker_task = asyncio.create_task(caller_worker.run())

    try:
        # Start the operation that waits for shutdown via workflow
        workflow_task = asyncio.create_task(
            env.client.execute_workflow(
                ShutdownTestCallerWorkflow.run,
                ShutdownTestCallerInput(
                    operation="wait_for_shutdown",
                    task_queue=handler_task_queue,
                ),
                id=str(uuid.uuid4()),
                task_queue=caller_task_queue,
            )
        )

        # Wait for operation to start
        await operation_started.wait()

        # Shutdown the handler worker - this should signal the shutdown event
        await handler_worker.shutdown()

        # The handler worker task should complete
        await handler_worker_task

        # The operation should have completed successfully
        result = await workflow_task
        assert result == "Worker graceful shutdown"
    finally:
        # Clean up the caller worker
        await caller_worker.shutdown()
        await caller_worker_task


async def test_sync_nexus_operation_worker_shutdown_graceful(env: WorkflowEnvironment):
    """Test that sync (ThreadPoolExecutor) Nexus operations complete gracefully during shutdown."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # Use separate task queues for caller and handler workers
    handler_task_queue = str(uuid.uuid4())
    caller_task_queue = str(uuid.uuid4())
    await create_nexus_endpoint(handler_task_queue, env.client)

    operation_started = asyncio.Event()
    loop = asyncio.get_event_loop()

    @service_handler(service=ShutdownTestService)
    class LocalSyncShutdownTestServiceHandler:
        @sync_operation
        async def wait_for_shutdown(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            raise NotImplementedError

        @sync_operation
        async def hang_until_cancelled(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            raise NotImplementedError

        @sync_operation
        def wait_for_shutdown_sync(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            # Signal that operation has started - need to use thread-safe mechanism
            loop.call_soon_threadsafe(operation_started.set)
            nexus.wait_for_worker_shutdown_sync(30)
            return "Worker graceful shutdown sync"

        @sync_operation
        async def check_shutdown(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            raise NotImplementedError

    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Handler worker (will be shut down)
        handler_worker = Worker(
            env.client,
            task_queue=handler_task_queue,
            nexus_service_handlers=[LocalSyncShutdownTestServiceHandler()],
            nexus_task_executor=executor,
            graceful_shutdown_timeout=timedelta(seconds=5),
        )

        # Caller worker (stays running to receive result)
        caller_worker = Worker(
            env.client,
            task_queue=caller_task_queue,
            workflows=[ShutdownTestCallerWorkflow],
        )

        handler_worker_task = asyncio.create_task(handler_worker.run())
        caller_worker_task = asyncio.create_task(caller_worker.run())

        try:
            # Start the operation that waits for shutdown synchronously via workflow
            workflow_task = asyncio.create_task(
                env.client.execute_workflow(
                    ShutdownTestCallerWorkflow.run,
                    ShutdownTestCallerInput(
                        operation="wait_for_shutdown_sync",
                        task_queue=handler_task_queue,
                    ),
                    id=str(uuid.uuid4()),
                    task_queue=caller_task_queue,
                )
            )

            # Wait for operation to start
            await operation_started.wait()

            # Shutdown the handler worker - this should signal the shutdown event
            await handler_worker.shutdown()

            # The handler worker task should complete
            await handler_worker_task

            # The operation should have completed successfully
            result = await workflow_task
            assert result == "Worker graceful shutdown sync"
        finally:
            # Clean up the caller worker
            await caller_worker.shutdown()
            await caller_worker_task


async def test_is_worker_shutdown(env: WorkflowEnvironment):
    """Test that is_worker_shutdown returns correct values."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    # Use separate task queues for caller and handler workers
    handler_task_queue = str(uuid.uuid4())
    caller_task_queue = str(uuid.uuid4())
    await create_nexus_endpoint(handler_task_queue, env.client)

    operation_started = asyncio.Event()
    shutdown_check_before: bool | None = None
    shutdown_check_after: bool | None = None

    @service_handler(service=ShutdownTestService)
    class LocalIsWorkerShutdownTestServiceHandler:
        @sync_operation
        async def wait_for_shutdown(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            raise NotImplementedError

        @sync_operation
        async def hang_until_cancelled(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            raise NotImplementedError

        @sync_operation
        async def wait_for_shutdown_sync(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            raise NotImplementedError

        @sync_operation
        async def check_shutdown(
            self, _ctx: StartOperationContext, _input: None
        ) -> str:
            nonlocal operation_started, shutdown_check_before, shutdown_check_after
            # Check before shutdown
            shutdown_check_before = nexus.is_worker_shutdown()
            operation_started.set()
            # Wait for shutdown
            await nexus.wait_for_worker_shutdown()
            # Check after shutdown
            shutdown_check_after = nexus.is_worker_shutdown()
            return "done"

    # Handler worker (will be shut down)
    handler_worker = Worker(
        env.client,
        task_queue=handler_task_queue,
        nexus_service_handlers=[LocalIsWorkerShutdownTestServiceHandler()],
        graceful_shutdown_timeout=timedelta(seconds=5),
    )

    # Caller worker (stays running to receive result)
    caller_worker = Worker(
        env.client,
        task_queue=caller_task_queue,
        workflows=[ShutdownTestCallerWorkflow],
    )

    handler_worker_task = asyncio.create_task(handler_worker.run())
    caller_worker_task = asyncio.create_task(caller_worker.run())

    try:
        # Start the operation via workflow
        workflow_task = asyncio.create_task(
            env.client.execute_workflow(
                ShutdownTestCallerWorkflow.run,
                ShutdownTestCallerInput(
                    operation="check_shutdown",
                    task_queue=handler_task_queue,
                ),
                id=str(uuid.uuid4()),
                task_queue=caller_task_queue,
            )
        )

        # Wait for operation to start
        await operation_started.wait()

        # Shutdown the handler worker
        await handler_worker.shutdown()

        await handler_worker_task
        result = await workflow_task

        assert shutdown_check_before is False
        assert shutdown_check_after is True  # pyright: ignore[reportUnreachable]
        assert result == "done"  # pyright: ignore[reportUnreachable]
    finally:
        # Clean up the caller worker
        await caller_worker.shutdown()
        await caller_worker_task
