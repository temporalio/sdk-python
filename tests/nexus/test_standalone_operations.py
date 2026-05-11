"""Integration tests for standalone Nexus operations (client-side).

Tests the client-side Nexus operation API: start, result, describe, cancel,
terminate, list, count, get_nexus_operation_handle, ID conflict policies,
and interceptor integration.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import nexusrpc
import pytest
from nexusrpc.handler import (
    StartOperationContext,
    service_handler,
    sync_operation,
)

from temporalio import nexus, workflow
from temporalio.client import (
    CancelNexusOperationInput,
    Client,
    CountNexusOperationsInput,
    DescribeNexusOperationInput,
    GetNexusOperationResultInput,
    Interceptor,
    ListNexusOperationsInput,
    NexusOperationExecutionDescription,
    NexusOperationFailureError,
    NexusOperationHandle,
    OutboundInterceptor,
    StartNexusOperationInput,
    TerminateNexusOperationInput,
    WorkflowUpdateStage,
)
from temporalio.common import (
    NexusOperationExecutionStatus,
    NexusOperationIDConflictPolicy,
    NexusOperationIDReusePolicy,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import (
    ApplicationError,
    CancelledError,
    NexusOperationAlreadyStartedError,
    TerminatedError,
)
from temporalio.nexus import WorkflowRunOperationContext, workflow_run_operation
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers import assert_eventually
from tests.helpers.nexus import make_nexus_endpoint_name

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class EchoInput:
    value: str


@dataclass
class EchoOutput:
    value: str


@dataclass
class RaiseErrInput:
    err_type: Literal["handler_err", "application_err"]


# ---------------------------------------------------------------------------
# Service definition
# ---------------------------------------------------------------------------


@nexusrpc.service
class StandaloneTestService:
    echo_sync: nexusrpc.Operation[EchoInput, EchoOutput]
    echo_async: nexusrpc.Operation[EchoInput, EchoOutput]
    blocking_async: nexusrpc.Operation[EchoInput, EchoOutput]
    raise_err: nexusrpc.Operation[RaiseErrInput, None]


@nexusrpc.service(name="StandaloneTestService")
class NamedService:
    echo_sync: nexusrpc.Operation[EchoInput, EchoOutput]


# ---------------------------------------------------------------------------
# Handler workflows
# ---------------------------------------------------------------------------


@workflow.defn
class EchoHandlerWorkflow:
    @workflow.run
    async def run(self, input: EchoInput) -> EchoOutput:
        return EchoOutput(value=input.value)


@workflow.defn
class BlockingHandlerWorkflow:
    """A workflow that blocks until it receives a signal or is cancelled/terminated."""

    def __init__(self) -> None:
        self._proceed = False

    @workflow.run
    async def run(self, input: EchoInput) -> EchoOutput:
        await workflow.wait_condition(lambda: self._proceed)
        return EchoOutput(value=input.value)

    @workflow.update
    def unblock(self) -> None:
        self._proceed = True


# ---------------------------------------------------------------------------
# Service handler
# ---------------------------------------------------------------------------


@service_handler(service=StandaloneTestService)
class StandaloneTestServiceHandler:
    def __init__(self) -> None:
        self.started_blocking = asyncio.Event()

    @sync_operation
    async def echo_sync(
        self, _ctx: StartOperationContext, input: EchoInput
    ) -> EchoOutput:
        return EchoOutput(value=input.value)

    @workflow_run_operation
    async def echo_async(
        self, ctx: WorkflowRunOperationContext, input: EchoInput
    ) -> nexus.WorkflowHandle[EchoOutput]:
        return await ctx.start_workflow(
            EchoHandlerWorkflow.run,
            input,
            id=str(uuid.uuid4()),
        )

    @workflow_run_operation
    async def blocking_async(
        self, ctx: WorkflowRunOperationContext, input: EchoInput
    ) -> nexus.WorkflowHandle[EchoOutput]:
        handle = await ctx.start_workflow(
            BlockingHandlerWorkflow.run,
            input,
            id=f"blocking_async-{input.value}",
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        )
        self.started_blocking.set()
        return handle

    @sync_operation
    async def raise_err(
        self, _ctx: StartOperationContext, input: RaiseErrInput
    ) -> None:
        match input.err_type:
            case "handler_err":
                raise nexusrpc.HandlerError(
                    "test handler error",
                    type=nexusrpc.HandlerErrorType.INTERNAL,
                    retryable_override=False,
                )
            case "application_err":
                raise ApplicationError("test application error", non_retryable=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_start_sync_operation_and_get_result(
    client: Client, env: WorkflowEnvironment
):
    """Start a sync nexus operation, call handle.result(), verify return value."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )
        # Use execute_with_retry to retry the full start+result cycle
        # (endpoint propagation may cause the first attempt to time out)
        handle = await nexus_client.start_operation(
            StandaloneTestService.echo_sync,
            EchoInput(value="hello"),
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        result = await handle.result()
        assert isinstance(result, EchoOutput)
        assert result.value == "hello"

        # test value is cached
        second_result = await handle.result()
        assert result is second_result


async def test_start_async_operation_and_poll_result(
    client: Client, env: WorkflowEnvironment
):
    """Start a workflow_run operation, poll result, verify."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )
        handle = await nexus_client.start_operation(
            StandaloneTestService.echo_async,
            EchoInput(value="async-hello"),
            id=str(uuid.uuid4()),
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        result = await handle.result()
        assert isinstance(result, EchoOutput)
        assert result.value == "async-hello"


async def test_execute_operation(client: Client, env: WorkflowEnvironment):
    """Use execute_operation convenience method, verify it returns result directly."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )
        result = await nexus_client.execute_operation(
            StandaloneTestService.echo_sync,
            EchoInput(value="execute"),
            id=str(uuid.uuid4()),
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        assert isinstance(result, EchoOutput)
        assert result.value == "execute"


async def test_execute_operation_named_service(
    client: Client, env: WorkflowEnvironment
):
    """Verify that the name on the service decorator is respected by the standalone nexus client"""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        # Register the standalone test service handler
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        # Create client using the service that is uses the name "StandaloneTestService"
        nexus_client = client.create_nexus_client(
            service=NamedService, endpoint=endpoint_name
        )
        result = await nexus_client.execute_operation(
            NamedService.echo_sync,
            EchoInput(value="execute"),
            id=str(uuid.uuid4()),
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=10),
        )
        assert isinstance(result, EchoOutput)
        assert result.value == "execute"


async def test_errors(client: Client, env: WorkflowEnvironment):
    """Execute operations that raise errors"""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )

        handle = await nexus_client.start_operation(
            StandaloneTestService.raise_err,
            RaiseErrInput("handler_err"),
            id=str(uuid.uuid4()),
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=30),
        )

        with pytest.raises(NexusOperationFailureError) as err:
            await handle.result()

        assert err.value.__cause__
        assert isinstance(err.value.__cause__, nexusrpc.HandlerError)

        # test that the error is cached
        with pytest.raises(NexusOperationFailureError) as second_err:
            await handle.result()
        assert err.value is second_err.value

        handle = await nexus_client.start_operation(
            StandaloneTestService.raise_err,
            RaiseErrInput("application_err"),
            id=str(uuid.uuid4()),
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=30),
        )

        with pytest.raises(NexusOperationFailureError) as err:
            await handle.result()

        assert err.value.__cause__
        assert isinstance(err.value.__cause__, nexusrpc.HandlerError)
        assert err.value.__cause__.__cause__
        assert isinstance(err.value.__cause__.__cause__, ApplicationError)


async def test_describe_operation(client: Client, env: WorkflowEnvironment):
    """Start op, get result first, then describe, verify fields populated."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )
        # Start an async operation and get its result first, then describe
        handle = await nexus_client.start_operation(
            StandaloneTestService.echo_async,
            EchoInput(value="describe-me"),
            id=str(uuid.uuid4()),
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=30),
            summary=StandaloneTestService.echo_async.name,
        )
        await handle.result()

        desc = await handle.describe()
        assert isinstance(desc, NexusOperationExecutionDescription)
        assert desc.operation_id == handle.operation_id
        assert desc.endpoint == endpoint_name
        assert desc.service == "StandaloneTestService"
        assert desc.operation == "echo_async"
        assert desc.status in (
            NexusOperationExecutionStatus.RUNNING,
            NexusOperationExecutionStatus.COMPLETED,
        )
        assert isinstance(desc.state, int)
        assert isinstance(desc.attempt, int)
        assert desc.blocked_reason is None or isinstance(desc.blocked_reason, str)
        assert desc.last_attempt_failure is None or isinstance(
            desc.last_attempt_failure, BaseException
        )
        summary = await desc.static_summary()
        assert summary == StandaloneTestService.echo_async.name


async def test_cancel_operation(client: Client, env: WorkflowEnvironment):
    """Start blocking async op, cancel it, verify awaiting result raises NexusOperationFailureError
    from a CancelledError.
    """
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )
        handle = await nexus_client.start_operation(
            StandaloneTestService.blocking_async,
            EchoInput(value="cancel-me"),
            id=str(uuid.uuid4()),
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=30),
        )

        # Cancel the operation
        await handle.cancel()

        with pytest.raises(NexusOperationFailureError) as err:
            await handle.result()

        assert err.value.__cause__
        assert isinstance(err.value.__cause__, CancelledError)


async def test_terminate_operation(client: Client, env: WorkflowEnvironment):
    """Start blocking async op, terminate it, verify awaiting the result raises NexusOperationFailureError
    from a TerminatedError.
    """
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )
        handle = await nexus_client.start_operation(
            StandaloneTestService.blocking_async,
            EchoInput(value="terminate-me"),
            id=str(uuid.uuid4()),
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=30),
        )

        # Terminate the operation
        await handle.terminate(reason="test termination")

        with pytest.raises(NexusOperationFailureError) as err:
            await handle.result()

        assert err.value.__cause__
        assert isinstance(err.value.__cause__, TerminatedError)


async def test_list_operations(client: Client, env: WorkflowEnvironment):
    """Start multiple ops, list them, verify iteration yields correct results."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )

        # Start several blocking operations so they remain visible
        op_ids: list[str] = []
        for i in range(3):
            op_id = str(uuid.uuid4())
            op_ids.append(op_id)
            await nexus_client.start_operation(
                StandaloneTestService.blocking_async,
                EchoInput(value=f"list-{i}"),
                id=op_id,
                id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
                schedule_to_close_timeout=timedelta(seconds=30),
            )

        # Poll until all 3 operations appear (visibility is eventually consistent)
        query = f'Endpoint = "{endpoint_name}"'

        async def check_ids() -> None:
            found_ids: set[str] = set()
            async for op_exec in client.list_nexus_operations(query):
                found_ids.add(op_exec.operation_id)
            assert all(op_id in found_ids for op_id in op_ids)

        await assert_eventually(check_ids)


async def test_count_operations(client: Client, env: WorkflowEnvironment):
    """Start ops, count, verify count."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )

        # Start some blocking operations
        for i in range(2):
            await nexus_client.start_operation(
                StandaloneTestService.blocking_async,
                EchoInput(value=f"count-{i}"),
                id=str(uuid.uuid4()),
                id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
                schedule_to_close_timeout=timedelta(seconds=30),
            )

        # Poll until count >= 2 (visibility is eventually consistent)
        query = f'Endpoint = "{endpoint_name}"'

        async def check_count() -> None:
            count_result = await client.count_nexus_operations(query)
            assert count_result.count >= 2

        await assert_eventually(check_count)


async def test_get_nexus_operation_handle(client: Client, env: WorkflowEnvironment):
    """Start op, get result, then get handle by ID and get result again."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )

        op_id = str(uuid.uuid4())
        original_handle = await nexus_client.start_operation(
            StandaloneTestService.echo_async,
            EchoInput(value="handle-test"),
            id=op_id,
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        # Get result from the original handle first
        original_result = await original_handle.result()
        assert isinstance(original_result, EchoOutput)
        assert original_result.value == "handle-test"

        # Get a fresh handle by ID and get result again
        handle = client.get_nexus_operation_handle(
            op_id, operation=StandaloneTestService.echo_async
        )
        result = await handle.result()
        assert isinstance(result, EchoOutput)
        assert result.value == "handle-test"


async def test_id_conflict_policy_use_existing(
    client: Client, env: WorkflowEnvironment
):
    """Start op, re-start with USE_EXISTING, verify same op/run ID and expected result"""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    service_handler = StandaloneTestServiceHandler()

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )

        op_id = str(uuid.uuid4())

        # First start
        handle = await nexus_client.start_operation(
            StandaloneTestService.blocking_async,
            EchoInput(value=task_queue),
            id=op_id,
            id_reuse_policy=NexusOperationIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.USE_EXISTING,
            schedule_to_close_timeout=timedelta(seconds=30),
        )

        # Second start with same ID and USE_EXISTING
        handle2 = await nexus_client.start_operation(
            StandaloneTestService.blocking_async,
            EchoInput(value="second"),
            id=op_id,
            id_reuse_policy=NexusOperationIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.USE_EXISTING,
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        assert handle.operation_id == handle2.operation_id
        assert handle.run_id == handle2.run_id

        # Let the nexus operation run and start the blocking workflow
        await service_handler.started_blocking.wait()

        expected_wf_id = f"blocking_async-{task_queue}"
        wf_handle = env.client.get_workflow_handle(expected_wf_id)

        await wf_handle.start_update(
            BlockingHandlerWorkflow.unblock,
            wait_for_stage=WorkflowUpdateStage.COMPLETED,
        )

        first_result = await handle.result()
        second_result = await handle2.result()
        assert first_result.value == task_queue
        assert first_result.value == second_result.value


async def test_id_conflict_policy_fail(client: Client, env: WorkflowEnvironment):
    """Start op, re-start with FAIL, verify raises NexusOperationAlreadyStartedError."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )

        op_id = str(uuid.uuid4())

        # First start
        await nexus_client.start_operation(
            StandaloneTestService.blocking_async,
            EchoInput(value="first"),
            id=op_id,
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=30),
        )

        # Second start with same ID and FAIL should raise
        with pytest.raises(NexusOperationAlreadyStartedError):
            await nexus_client.start_operation(
                StandaloneTestService.blocking_async,
                EchoInput(value="second"),
                id=op_id,
                id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
                schedule_to_close_timeout=timedelta(seconds=30),
            )


# ---------------------------------------------------------------------------
# Interceptor test
# ---------------------------------------------------------------------------


class _RecordingOutboundInterceptor(OutboundInterceptor):
    """Outbound interceptor that records calls to nexus operation methods."""

    def __init__(
        self, next: OutboundInterceptor, parent: _RecordingInterceptor
    ) -> None:
        super().__init__(next)
        self._parent = parent

    async def start_nexus_operation(
        self, input: StartNexusOperationInput
    ) -> NexusOperationHandle[Any]:
        self._parent.start_calls.append(input)
        return await super().start_nexus_operation(input)

    async def describe_nexus_operation(
        self, input: DescribeNexusOperationInput
    ) -> NexusOperationExecutionDescription:
        self._parent.describe_calls.append(input)
        return await super().describe_nexus_operation(input)

    async def get_nexus_operation_result(
        self, input: GetNexusOperationResultInput
    ) -> Any:
        self._parent.result_calls.append(input)
        return await super().get_nexus_operation_result(input)

    async def cancel_nexus_operation(self, input: CancelNexusOperationInput) -> None:
        self._parent.cancel_calls.append(input)
        return await super().cancel_nexus_operation(input)

    async def terminate_nexus_operation(
        self, input: TerminateNexusOperationInput
    ) -> None:
        self._parent.terminate_calls.append(input)
        return await super().terminate_nexus_operation(input)

    def list_nexus_operations(self, input: ListNexusOperationsInput):
        self._parent.list_calls.append(input)
        return super().list_nexus_operations(input)

    async def count_nexus_operations(self, input: CountNexusOperationsInput):
        self._parent.count_calls.append(input)
        return await super().count_nexus_operations(input)


class _RecordingInterceptor(Interceptor):
    """Client interceptor that records nexus operation calls."""

    def __init__(self) -> None:
        super().__init__()
        self.start_calls: list[StartNexusOperationInput] = []
        self.describe_calls: list[DescribeNexusOperationInput] = []
        self.result_calls: list[GetNexusOperationResultInput] = []
        self.cancel_calls: list[CancelNexusOperationInput] = []
        self.terminate_calls: list[TerminateNexusOperationInput] = []
        self.list_calls: list[ListNexusOperationsInput] = []
        self.count_calls: list[CountNexusOperationsInput] = []

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return _RecordingOutboundInterceptor(next, self)


async def test_interceptor_receives_inputs(client: Client, env: WorkflowEnvironment):
    """Custom OutboundInterceptor records calls, verify correct input types."""
    if env.supports_time_skipping:
        pytest.skip(
            "Standalone Nexus Operation tests don't work with time-skipping server"
        )

    task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(task_queue)

    interceptor = _RecordingInterceptor()
    intercepted_client = Client(
        service_client=client.service_client,
        namespace=client.namespace,
        interceptors=[interceptor],
    )

    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[StandaloneTestServiceHandler()],
        workflows=[EchoHandlerWorkflow, BlockingHandlerWorkflow],
    ):
        await env.create_nexus_endpoint(endpoint_name, task_queue)

        nexus_client = intercepted_client.create_nexus_client(
            service=StandaloneTestService, endpoint=endpoint_name
        )

        op_id = str(uuid.uuid4())

        # Start operation -- should trigger start interceptor (with retry)
        handle = await nexus_client.start_operation(
            StandaloneTestService.blocking_async,
            EchoInput(value=f"interceptor-test-{op_id}"),
            id=op_id,
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=30),
            schedule_to_start_timeout=timedelta(seconds=5),
            start_to_close_timeout=timedelta(seconds=20),
        )
        assert len(interceptor.start_calls) >= 1
        start_input = interceptor.start_calls[-1]
        assert isinstance(start_input, StartNexusOperationInput)
        assert start_input.id == op_id
        assert start_input.operation == "blocking_async"
        assert start_input.endpoint == endpoint_name
        assert start_input.service == "StandaloneTestService"
        assert start_input.schedule_to_start_timeout == timedelta(seconds=5)
        assert start_input.start_to_close_timeout == timedelta(seconds=20)

        # Describe
        await handle.describe()
        assert len(interceptor.describe_calls) == 1
        desc_input = interceptor.describe_calls[0]
        assert isinstance(desc_input, DescribeNexusOperationInput)
        assert desc_input.operation_id == op_id

        # Cancel
        await handle.cancel()
        assert len(interceptor.cancel_calls) == 1
        cancel_input = interceptor.cancel_calls[0]
        assert isinstance(cancel_input, CancelNexusOperationInput)
        assert cancel_input.operation_id == op_id

        # GetResult
        with pytest.raises(NexusOperationFailureError):
            await handle.result()
        assert len(interceptor.result_calls) == 1
        result_input = interceptor.result_calls[0]
        assert isinstance(result_input, GetNexusOperationResultInput)
        assert result_input.operation_id == op_id
        assert result_input.result_type == EchoOutput

        # Start another so we can terminate it
        previous_start_count = len(interceptor.start_calls)
        op_id = str(uuid.uuid4())
        handle = await nexus_client.start_operation(
            StandaloneTestService.blocking_async,
            EchoInput(value=f"interceptor-test-{op_id}"),
            id=op_id,
            id_reuse_policy=NexusOperationIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=NexusOperationIDConflictPolicy.FAIL,
            schedule_to_close_timeout=timedelta(seconds=30),
        )
        assert len(interceptor.start_calls) > previous_start_count

        # Terminate
        await handle.terminate()
        assert len(interceptor.terminate_calls) == 1
        terminate_input = interceptor.terminate_calls[0]
        assert isinstance(terminate_input, TerminateNexusOperationInput)
        assert terminate_input.operation_id == op_id

        query = f'`OperationId`="{op_id}"'

        # List Operations
        # Iterate over list to ensure call is made
        async for _ in intercepted_client.list_nexus_operations(query):
            pass
        assert len(interceptor.list_calls) >= 1
        list_input = interceptor.list_calls[-1]
        assert isinstance(list_input, ListNexusOperationsInput)
        assert list_input.query == query

        # Count Operations
        await intercepted_client.count_nexus_operations(query)
        assert len(interceptor.count_calls) >= 1
        count_input = interceptor.count_calls[-1]
        assert isinstance(count_input, CountNexusOperationsInput)
        assert count_input.query == query
