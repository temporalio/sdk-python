"""Integration tests for external storage with Nexus task processing.

Mirrors sdk-typescript's test-integration-extstore-nexus. A caller workflow
invokes a Nexus operation whose input and/or result is large enough to be
offloaded to external storage. Verifies that the offloaded input is retrieved
before the handler runs, that a large synchronous result is offloaded when
completing the task (and retrieved by the caller), and that a transient
storage-driver failure fails the Nexus task retryably and then recovers.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import nexusrpc
import pytest
from nexusrpc.handler import (
    StartOperationContext,
    service_handler,
    sync_operation,
)

import temporalio.converter
from temporalio import workflow
from temporalio.api.common.v1 import Payload
from temporalio.client import Client, WorkflowFailureError
from temporalio.converter import (
    ExternalStorage,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
)
from temporalio.exceptions import ApplicationError, NexusOperationError
from temporalio.testing import WorkflowEnvironment
from temporalio.types import MethodAsyncSingleParam
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from tests.helpers.nexus import make_nexus_endpoint_name
from tests.test_extstore import InMemoryTestDriver

pytestmark = pytest.mark.requires_local_server

PAYLOAD_SIZE = 4096
PAYLOAD_SIZE_THRESHOLD = 1024
_STORE_FAILURE_MESSAGE = "external storage store failed"


@nexusrpc.service
class ExtStoreNexusService:
    size_op: nexusrpc.Operation[str, int]
    big_result_op: nexusrpc.Operation[int, str]


@service_handler(service=ExtStoreNexusService)
class ExtStoreNexusServiceHandler:
    @sync_operation
    async def size_op(self, _ctx: StartOperationContext, data: str) -> int:
        return len(data)

    @sync_operation
    async def big_result_op(self, _ctx: StartOperationContext, size: int) -> str:
        return "x" * size


@workflow.defn
class SizeOpCallerWorkflow:
    """Calls ``size_op`` with a large input (offloaded) and returns its length."""

    @workflow.run
    async def run(self, task_queue: str) -> int:
        nexus_client = workflow.create_nexus_client(
            service=ExtStoreNexusService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        return await nexus_client.execute_operation(
            ExtStoreNexusService.size_op, "x" * PAYLOAD_SIZE
        )


@workflow.defn
class BigResultOpCallerWorkflow:
    """Calls ``big_result_op`` (whose result is offloaded) and returns its length."""

    @workflow.run
    async def run(self, task_queue: str) -> int:
        nexus_client = workflow.create_nexus_client(
            service=ExtStoreNexusService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        result = await nexus_client.execute_operation(
            ExtStoreNexusService.big_result_op, PAYLOAD_SIZE
        )
        return len(result)


class TransientFailureDriver(InMemoryTestDriver):
    """In-memory driver that fails the first store and/or retrieve call, then
    behaves normally. Simulates a transient storage-driver outage."""

    def __init__(
        self,
        *,
        fail_first_store: bool = False,
        fail_first_retrieve: bool = False,
        driver_name: str = "test-driver",
    ):
        super().__init__(driver_name=driver_name)
        self._fail_first_store = fail_first_store
        self._fail_first_retrieve = fail_first_retrieve
        self.store_attempts = 0
        self.retrieve_attempts = 0

    async def store(
        self,
        context: StorageDriverStoreContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        self.store_attempts += 1
        if self._fail_first_store and self.store_attempts == 1:
            raise RuntimeError("transient store failure")
        return await super().store(context, payloads)

    async def retrieve(
        self,
        context: StorageDriverRetrieveContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        self.retrieve_attempts += 1
        if self._fail_first_retrieve and self.retrieve_attempts == 1:
            raise RuntimeError("transient retrieve failure")
        return await super().retrieve(context, claims)


def _client_with_extstore(
    env: WorkflowEnvironment, driver: InMemoryTestDriver
) -> Client:
    config = env.client.config()
    config["data_converter"] = dataclasses.replace(
        temporalio.converter.default(),
        external_storage=ExternalStorage(
            drivers=[driver],
            payload_size_threshold=PAYLOAD_SIZE_THRESHOLD,
        ),
    )
    return Client(**config)


async def _run_caller(
    env: WorkflowEnvironment,
    driver: InMemoryTestDriver,
    workflow_run: MethodAsyncSingleParam[Any, str, int],
) -> int:
    client = _client_with_extstore(env, driver)
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[SizeOpCallerWorkflow, BigResultOpCallerWorkflow],
        nexus_service_handlers=[ExtStoreNexusServiceHandler()],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
        return await client.execute_workflow(
            workflow_run,
            task_queue,
            id=str(uuid.uuid4()),
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=30),
        )


def _cause_chain(err: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    e: BaseException | None = err
    while e is not None:
        chain.append(e)
        e = e.__cause__
    return chain


async def test_nexus_operation_input_offloaded_and_retrieved(env: WorkflowEnvironment):
    """The offloaded operation input is retrieved before the handler runs."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the Java test server")

    driver = InMemoryTestDriver()
    result = await _run_caller(env, driver, SizeOpCallerWorkflow.run)

    assert result == PAYLOAD_SIZE
    assert driver._store_calls >= 1
    assert driver._retrieve_calls >= 1


async def test_nexus_operation_sync_result_offloaded_and_retrieved(
    env: WorkflowEnvironment,
):
    """A large synchronous result is offloaded and retrieved by the caller."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the Java test server")

    driver = InMemoryTestDriver()
    result = await _run_caller(env, driver, BigResultOpCallerWorkflow.run)

    assert result == PAYLOAD_SIZE
    assert driver._store_calls >= 1
    assert driver._retrieve_calls >= 1


async def test_nexus_operation_transient_retrieve_failure_recovers(
    env: WorkflowEnvironment,
):
    """A transient retrieve failure fails the task retryably; it then recovers."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the Java test server")

    driver = TransientFailureDriver(fail_first_retrieve=True)
    result = await _run_caller(env, driver, SizeOpCallerWorkflow.run)

    assert result == PAYLOAD_SIZE
    assert driver.retrieve_attempts >= 2


async def test_nexus_operation_transient_store_failure_recovers(
    env: WorkflowEnvironment,
):
    """A transient store failure fails the task retryably; it then recovers."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the Java test server")

    driver = TransientFailureDriver(fail_first_store=True)
    result = await _run_caller(env, driver, BigResultOpCallerWorkflow.run)

    assert result == PAYLOAD_SIZE
    assert driver.store_attempts >= 2


class PermanentFailStoreDriver(InMemoryTestDriver):
    """Store always fails non-retryably, so the Nexus operation fails permanently."""

    async def store(
        self,
        context: StorageDriverStoreContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        raise ApplicationError(_STORE_FAILURE_MESSAGE, non_retryable=True)


async def test_nexus_operation_store_failure_fails_operation(
    env: WorkflowEnvironment,
):
    """A non-retryable store failure fails the operation and surfaces the driver
    error to the caller (deterministically, with no retries)."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the Java test server")

    driver = PermanentFailStoreDriver()
    with pytest.raises(WorkflowFailureError) as exc_info:
        await _run_caller(env, driver, BigResultOpCallerWorkflow.run)

    causes = _cause_chain(exc_info.value)
    assert [type(c) for c in causes] == [
        WorkflowFailureError,
        NexusOperationError,
        nexusrpc.HandlerError,
        ApplicationError,
    ]
    assert _STORE_FAILURE_MESSAGE in str(causes[-1])
