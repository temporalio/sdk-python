"""Tests for Nexus worker shutdown support."""

import asyncio
import concurrent.futures
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import nexusrpc
import pytest
from nexusrpc.handler import (
    CancelOperationContext,
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    operation_handler,
    service_handler,
    sync_operation,
)

from temporalio import nexus, workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client
from temporalio.converter import DataConverter, PayloadCodec
from temporalio.exceptions import ApplicationError
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
    fail_with_details: nexusrpc.Operation[None, str]
    fail_cancel_with_details: nexusrpc.Operation[None, str]
    fail_with_unserializable_details: nexusrpc.Operation[None, str]


class FailCancelWithDetailsHandler(OperationHandler[None, str]):
    async def start(
        self, ctx: StartOperationContext, input: None
    ) -> StartOperationResultAsync:
        return StartOperationResultAsync(token="token")

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        raise ApplicationError("Cancel failed", "some details")


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

    @sync_operation
    async def fail_with_details(self, _ctx: StartOperationContext, _input: None) -> str:
        # The details give the failure a payload for the codec to encode
        raise ApplicationError("Operation failed", "some details")

    @operation_handler
    def fail_cancel_with_details(self) -> OperationHandler[None, str]:
        return FailCancelWithDetailsHandler()

    @sync_operation
    async def fail_with_unserializable_details(
        self, _ctx: StartOperationContext, _input: None
    ) -> str:
        assert self.operation_started
        self.operation_started.set()
        # The failure converter can't turn these details into a payload
        raise ApplicationError("Operation failed", object())


@dataclass
class ShutdownTestCallerInput:
    operation: Literal[
        "wait_for_shutdown",
        "hang_until_cancelled",
        "wait_for_shutdown_sync",
        "check_shutdown",
        "fail_with_details",
        "fail_cancel_with_details",
        "fail_with_unserializable_details",
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
            case "fail_with_details":
                return await nexus_client.execute_operation(
                    ShutdownTestService.fail_with_details, None
                )
            case "fail_cancel_with_details":
                handle = await nexus_client.start_operation(
                    ShutdownTestService.fail_cancel_with_details, None
                )
                handle.cancel()
                return await handle
            case "fail_with_unserializable_details":
                return await nexus_client.execute_operation(
                    ShutdownTestService.fail_with_unserializable_details, None
                )


async def test_nexus_worker_shutdown(env: WorkflowEnvironment):
    """Test that Nexus operations are cancelled when worker shuts down without graceful timeout."""
    if env.supports_time_skipping_v1:
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
    if env.supports_time_skipping_v1:
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
    if env.supports_time_skipping_v1:
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
    if env.supports_time_skipping_v1:
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


class SlowEncodePayloadCodec(PayloadCodec):
    def __init__(self) -> None:
        self.encode_started = asyncio.Event()

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        self.encode_started.set()
        # Block until the worker shutdown cancels the task mid-encode
        await asyncio.Future()
        return list(payloads)

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return list(payloads)


@pytest.mark.parametrize("operation", ["fail_with_details", "fail_cancel_with_details"])
async def test_nexus_worker_shutdown_while_encoding_failure(
    env: WorkflowEnvironment,
    operation: Literal["fail_with_details", "fail_cancel_with_details"],
):
    """Test that shutdown doesn't hang when it cancels a task mid-encode."""
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with time-skipping server")

    handler_task_queue = str(uuid.uuid4())
    caller_task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(
        make_nexus_endpoint_name(handler_task_queue), handler_task_queue
    )

    codec = SlowEncodePayloadCodec()
    handler_client_config = env.client.config()
    handler_client_config["data_converter"] = DataConverter(payload_codec=codec)

    with concurrent.futures.ThreadPoolExecutor() as executor:
        async with Worker(
            env.client,
            task_queue=caller_task_queue,
            workflows=[ShutdownTestCallerWorkflow],
        ):
            handler_worker = Worker(
                Client(**handler_client_config),
                task_queue=handler_task_queue,
                nexus_service_handlers=[ShutdownTestServiceHandler()],
                nexus_task_executor=executor,
            )

            handler_worker_task = asyncio.create_task(handler_worker.run())

            await env.client.start_workflow(
                ShutdownTestCallerWorkflow.run,
                ShutdownTestCallerInput(
                    operation=operation,
                    task_queue=handler_task_queue,
                ),
                id=str(uuid.uuid4()),
                task_queue=caller_task_queue,
            )

            await codec.encode_started.wait()

            await asyncio.wait_for(handler_worker.shutdown(), 10)
            await handler_worker_task


class FailingEncodePayloadCodec(PayloadCodec):
    def __init__(self) -> None:
        self.encode_called = asyncio.Event()

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        self.encode_called.set()
        raise RuntimeError("Codec failed")

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return list(payloads)


@pytest.mark.parametrize(
    "operation",
    [
        "fail_with_details",
        "fail_cancel_with_details",
        "fail_with_unserializable_details",
    ],
)
async def test_nexus_worker_shutdown_after_failure_build_error(
    env: WorkflowEnvironment,
    operation: Literal[
        "fail_with_details",
        "fail_cancel_with_details",
        "fail_with_unserializable_details",
    ],
):
    """Test that shutdown doesn't hang when a handler failure can't be converted or encoded."""
    if env.supports_time_skipping_v1:
        pytest.skip("Nexus tests don't work with time-skipping server")

    handler_task_queue = str(uuid.uuid4())
    caller_task_queue = str(uuid.uuid4())
    await env.create_nexus_endpoint(
        make_nexus_endpoint_name(handler_task_queue), handler_task_queue
    )

    # The unserializable details fail in the failure converter, the others in
    # the codec
    codec = FailingEncodePayloadCodec()
    handler_client_config = env.client.config()
    if operation != "fail_with_unserializable_details":
        handler_client_config["data_converter"] = DataConverter(payload_codec=codec)
    operation_started = asyncio.Event()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        async with Worker(
            env.client,
            task_queue=caller_task_queue,
            workflows=[ShutdownTestCallerWorkflow],
        ):
            handler_worker = Worker(
                Client(**handler_client_config),
                task_queue=handler_task_queue,
                nexus_service_handlers=[ShutdownTestServiceHandler(operation_started)],
                nexus_task_executor=executor,
            )

            handler_worker_task = asyncio.create_task(handler_worker.run())

            await env.client.start_workflow(
                ShutdownTestCallerWorkflow.run,
                ShutdownTestCallerInput(
                    operation=operation,
                    task_queue=handler_task_queue,
                ),
                id=str(uuid.uuid4()),
                task_queue=caller_task_queue,
            )

            if operation == "fail_with_unserializable_details":
                await operation_started.wait()
            else:
                await codec.encode_called.wait()

            await asyncio.wait_for(handler_worker.shutdown(), 10)
            await handler_worker_task
