import asyncio
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, Union

import nexusrpc
import nexusrpc.handler
import pytest
from nexusrpc.handler import (
    CancelOperationContext,
    FetchOperationInfoContext,
    StartOperationContext,
)

import temporalio.api
import temporalio.api.common
import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.nexus
import temporalio.api.nexus.v1
import temporalio.api.operatorservice
import temporalio.api.operatorservice.v1
import temporalio.nexus
import temporalio.nexus.handler
from temporalio import workflow
from temporalio.client import (
    Client,
    WithStartWorkflowOperation,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
)
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import CancelledError, NexusHandlerError, NexusOperationError
from temporalio.nexus.handler import TemporalOperationContext, WorkflowOperationToken
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from tests.helpers.nexus import create_nexus_endpoint, make_nexus_endpoint_name

# TODO(dan): test availability of Temporal client etc in async context set by worker
# TODO(dan): test worker shutdown, wait_all_completed, drain etc
# TODO(dan): test worker op handling failure

# -----------------------------------------------------------------------------
# Test definition
#


class CallerReference(IntEnum):
    IMPL_WITHOUT_INTERFACE = 0
    IMPL_WITH_INTERFACE = 1
    INTERFACE = 2


class OpDefinitionType(IntEnum):
    SHORTHAND = 0
    LONGHAND = 1


@dataclass
class SyncResponse:
    op_definition_type: OpDefinitionType
    use_async_def: bool
    exception_in_operation_start: bool


@dataclass
class AsyncResponse:
    operation_workflow_id: str
    block_forever_waiting_for_cancellation: bool
    op_definition_type: OpDefinitionType
    exception_in_operation_start: bool


# The order of the two types in this union is critical since the data converter matches
# eagerly, ignoring unknown fields, and so would identify an AsyncResponse as a
# SyncResponse if SyncResponse came first.
ResponseType = Union[AsyncResponse, SyncResponse]

# -----------------------------------------------------------------------------
# Service interface
#


@dataclass
class OpInput:
    response_type: ResponseType
    headers: dict[str, str]
    caller_reference: CallerReference


@dataclass
class OpOutput:
    value: str


@dataclass
class HandlerWfInput:
    op_input: OpInput


@dataclass
class HandlerWfOutput:
    value: str


@nexusrpc.service
class ServiceInterface:
    sync_or_async_operation: nexusrpc.Operation[OpInput, OpOutput]
    sync_operation: nexusrpc.Operation[OpInput, OpOutput]
    async_operation: nexusrpc.Operation[OpInput, HandlerWfOutput]


# -----------------------------------------------------------------------------
# Service implementation
#


@workflow.defn
class HandlerWorkflow:
    @workflow.run
    async def run(
        self,
        input: HandlerWfInput,
    ) -> HandlerWfOutput:
        assert isinstance(input.op_input.response_type, AsyncResponse)
        if input.op_input.response_type.block_forever_waiting_for_cancellation:
            await asyncio.Future()
        return HandlerWfOutput(
            value="workflow result",
        )


# TODO: make types pass pyright strict mode


class SyncOrAsyncOperation(nexusrpc.handler.OperationHandler[OpInput, OpOutput]):
    async def start(
        self, ctx: StartOperationContext, input: OpInput
    ) -> Union[
        nexusrpc.handler.StartOperationResultSync[OpOutput],
        nexusrpc.handler.StartOperationResultAsync,
    ]:
        if input.response_type.exception_in_operation_start:
            # TODO(dan): don't think RPCError should be used here
            raise RPCError(
                "RPCError INVALID_ARGUMENT in Nexus operation",
                RPCStatusCode.INVALID_ARGUMENT,
                b"",
            )
        if isinstance(input.response_type, SyncResponse):
            return nexusrpc.handler.StartOperationResultSync(
                value=OpOutput(value="sync response")
            )
        elif isinstance(input.response_type, AsyncResponse):
            tctx = TemporalOperationContext.current()
            token = await tctx.start_workflow(
                HandlerWorkflow.run,
                HandlerWfInput(op_input=input),
                id=input.response_type.operation_workflow_id,
            )
            return nexusrpc.handler.StartOperationResultAsync(token.encode())
        else:
            raise TypeError

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        return await temporalio.nexus.handler.cancel_operation(token)

    async def fetch_info(
        self, ctx: FetchOperationInfoContext, token: str
    ) -> nexusrpc.handler.OperationInfo:
        raise NotImplementedError

    async def fetch_result(
        self, ctx: nexusrpc.handler.FetchOperationResultContext, token: str
    ) -> OpOutput:
        raise NotImplementedError


@nexusrpc.handler.service_handler(service=ServiceInterface)
class ServiceImpl:
    @nexusrpc.handler.operation_handler
    def sync_or_async_operation(
        self,
    ) -> nexusrpc.handler.OperationHandler[OpInput, OpOutput]:
        return SyncOrAsyncOperation()

    @nexusrpc.handler.operation_handler
    def sync_operation(
        self,
    ) -> nexusrpc.handler.OperationHandler[OpInput, OpOutput]:
        async def start(ctx: StartOperationContext, input: OpInput) -> OpOutput:
            assert isinstance(input.response_type, SyncResponse)
            if input.response_type.exception_in_operation_start:
                raise RPCError(
                    "RPCError INVALID_ARGUMENT in Nexus operation",
                    RPCStatusCode.INVALID_ARGUMENT,
                    b"",
                )
            return OpOutput(value="sync response")

        return nexusrpc.handler.SyncOperationHandler(start)

    @nexusrpc.handler.operation_handler
    def async_operation(
        self,
    ) -> nexusrpc.handler.OperationHandler[OpInput, HandlerWfOutput]:
        async def start(
            ctx: StartOperationContext, input: OpInput
        ) -> WorkflowOperationToken[HandlerWfOutput]:
            assert isinstance(input.response_type, AsyncResponse)
            if input.response_type.exception_in_operation_start:
                raise RPCError(
                    "RPCError INVALID_ARGUMENT in Nexus operation",
                    RPCStatusCode.INVALID_ARGUMENT,
                    b"",
                )
            tctx = TemporalOperationContext.current()
            return await tctx.start_workflow(
                HandlerWorkflow.run,
                HandlerWfInput(op_input=input),
                id=input.response_type.operation_workflow_id,
            )

        return temporalio.nexus.handler.WorkflowRunOperationHandler(start)


# -----------------------------------------------------------------------------
# Caller workflow
#


@dataclass
class CallerWfInput:
    op_input: OpInput


@dataclass
class CallerWfOutput:
    op_output: OpOutput


@workflow.defn
class CallerWorkflow:
    """
    A workflow that executes a Nexus operation, specifying whether it should return
    synchronously or asynchronously.
    """

    @workflow.init
    def __init__(
        self,
        input: CallerWfInput,
        request_cancel: bool,
        task_queue: str,
    ) -> None:
        self.nexus_client = workflow.NexusClient(
            service={
                CallerReference.IMPL_WITH_INTERFACE: ServiceImpl,
                CallerReference.INTERFACE: ServiceInterface,
            }[input.op_input.caller_reference],
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        self._nexus_operation_started = False
        self._proceed = False

    @workflow.run
    async def run(
        self,
        input: CallerWfInput,
        request_cancel: bool,
        task_queue: str,
    ) -> CallerWfOutput:
        op_input = input.op_input
        op_handle = await self.nexus_client.start_operation(
            self._get_operation(op_input),
            op_input,
            headers=op_input.headers,
        )
        self._nexus_operation_started = True
        if not input.op_input.response_type.exception_in_operation_start:
            if isinstance(input.op_input.response_type, SyncResponse):
                assert (
                    op_handle.operation_token is None
                ), "operation_token should be absent after a sync response"
            else:
                assert (
                    op_handle.operation_token
                ), "operation_token should be present after an async response"

        if request_cancel:
            # Even for SyncResponse, the op_handle future is not done at this point; that
            # transition doesn't happen until the handle is awaited.
            assert op_handle.cancel()
        op_output = await op_handle
        return CallerWfOutput(op_output=OpOutput(value=op_output.value))

    @workflow.update
    async def wait_nexus_operation_started(self) -> None:
        await workflow.wait_condition(lambda: self._nexus_operation_started)

    @staticmethod
    def _get_operation(
        op_input: OpInput,
    ) -> Union[
        nexusrpc.Operation[OpInput, OpOutput],
        Callable[[Any], nexusrpc.handler.OperationHandler[OpInput, OpOutput]],
    ]:
        return {
            (
                SyncResponse,
                OpDefinitionType.SHORTHAND,
                CallerReference.IMPL_WITH_INTERFACE,
                True,
            ): ServiceImpl.sync_operation,
            (
                SyncResponse,
                OpDefinitionType.SHORTHAND,
                CallerReference.INTERFACE,
                True,
            ): ServiceInterface.sync_operation,
            (
                SyncResponse,
                OpDefinitionType.LONGHAND,
                CallerReference.IMPL_WITH_INTERFACE,
                True,
            ): ServiceImpl.sync_or_async_operation,
            (
                SyncResponse,
                OpDefinitionType.LONGHAND,
                CallerReference.INTERFACE,
                True,
            ): ServiceInterface.sync_or_async_operation,
            (
                AsyncResponse,
                OpDefinitionType.SHORTHAND,
                CallerReference.IMPL_WITH_INTERFACE,
                True,
            ): ServiceImpl.async_operation,
            (
                AsyncResponse,
                OpDefinitionType.SHORTHAND,
                CallerReference.INTERFACE,
                True,
            ): ServiceInterface.async_operation,
            (
                AsyncResponse,
                OpDefinitionType.LONGHAND,
                CallerReference.IMPL_WITH_INTERFACE,
                True,
            ): ServiceImpl.sync_or_async_operation,
            (
                AsyncResponse,
                OpDefinitionType.LONGHAND,
                CallerReference.INTERFACE,
                True,
            ): ServiceInterface.sync_or_async_operation,
        }[
            {True: SyncResponse, False: AsyncResponse}[
                isinstance(op_input.response_type, SyncResponse)
            ],
            op_input.response_type.op_definition_type,
            op_input.caller_reference,
            (
                op_input.response_type.use_async_def
                if isinstance(op_input.response_type, SyncResponse)
                else True
            ),
        ]


@workflow.defn
class UntypedCallerWorkflow:
    @workflow.init
    def __init__(
        self, input: CallerWfInput, request_cancel: bool, task_queue: str
    ) -> None:
        # TODO(dan): untyped caller cannot reference name of implementation. I think this is as it should be.
        service_name = "ServiceInterface"
        self.nexus_client = workflow.NexusClient(
            service=service_name,
            endpoint=make_nexus_endpoint_name(task_queue),
        )

    @workflow.run
    async def run(
        self, input: CallerWfInput, request_cancel: bool, task_queue: str
    ) -> CallerWfOutput:
        op_input = input.op_input
        if op_input.response_type.op_definition_type == OpDefinitionType.LONGHAND:
            op_name = "sync_or_async_operation"
        elif isinstance(op_input.response_type, AsyncResponse):
            op_name = "async_operation"
        elif isinstance(op_input.response_type, SyncResponse):
            op_name = "sync_operation"
        else:
            raise TypeError

        arbitrary_condition = isinstance(op_input.response_type, SyncResponse)

        if arbitrary_condition:
            op_handle = await self.nexus_client.start_operation(
                op_name,
                op_input,
                headers=op_input.headers,
                output_type=OpOutput,
            )
            op_output = await op_handle
        else:
            op_output = await self.nexus_client.execute_operation(
                op_name,
                op_input,
                headers=op_input.headers,
                output_type=OpOutput,
            )
        return CallerWfOutput(op_output=OpOutput(value=op_output.value))


# -----------------------------------------------------------------------------
# Tests
#


# TODO(dan): cross-namespace tests
# TODO(dan): nexus endpoint pytest fixture?
# TODO(dan): test headers
@pytest.mark.parametrize("exception_in_operation_start", [False, True])
@pytest.mark.parametrize("request_cancel", [False, True])
@pytest.mark.parametrize(
    "op_definition_type", [OpDefinitionType.SHORTHAND, OpDefinitionType.LONGHAND]
)
@pytest.mark.parametrize(
    "caller_reference",
    [CallerReference.IMPL_WITH_INTERFACE, CallerReference.INTERFACE],
)
async def test_sync_response(
    client: Client,
    exception_in_operation_start: bool,
    request_cancel: bool,
    op_definition_type: OpDefinitionType,
    caller_reference: CallerReference,
):
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[ServiceImpl()],
        workflows=[CallerWorkflow, HandlerWorkflow],
        task_queue=task_queue,
        # TODO(dan): enable sandbox
        workflow_runner=UnsandboxedWorkflowRunner(),
        workflow_failure_exception_types=[Exception],
    ):
        await create_nexus_endpoint(task_queue, client)
        caller_wf_handle = await client.start_workflow(
            CallerWorkflow.run,
            args=[
                CallerWfInput(
                    op_input=OpInput(
                        response_type=SyncResponse(
                            op_definition_type=op_definition_type,
                            use_async_def=True,
                            exception_in_operation_start=exception_in_operation_start,
                        ),
                        headers={"header-key": "header-value"},
                        caller_reference=caller_reference,
                    ),
                ),
                request_cancel,
                task_queue,
            ],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )

        # TODO(dan): check bidi links for sync operation

        # The operation result is returned even when request_cancel=True, because the
        # response was synchronous and it could not be cancelled. See explanation below.
        if exception_in_operation_start:
            with pytest.raises(WorkflowFailureError) as ei:
                await caller_wf_handle.result()
            e = ei.value
            assert isinstance(e, WorkflowFailureError)
            assert isinstance(e.__cause__, NexusOperationError)
            assert isinstance(e.__cause__.__cause__, NexusHandlerError)
            # ID of first command
            assert e.__cause__.scheduled_event_id == 5
            assert e.__cause__.endpoint == make_nexus_endpoint_name(task_queue)
            assert e.__cause__.service == "ServiceInterface"
            assert (
                e.__cause__.operation == "sync_operation"
                if op_definition_type == OpDefinitionType.SHORTHAND
                else "sync_or_async_operation"
            )
        else:
            result = await caller_wf_handle.result()
            assert result.op_output.value == "sync response"


@pytest.mark.parametrize("exception_in_operation_start", [False, True])
@pytest.mark.parametrize("request_cancel", [False, True])
@pytest.mark.parametrize(
    "op_definition_type", [OpDefinitionType.SHORTHAND, OpDefinitionType.LONGHAND]
)
@pytest.mark.parametrize(
    "caller_reference",
    [CallerReference.IMPL_WITH_INTERFACE, CallerReference.INTERFACE],
)
async def test_async_response(
    client: Client,
    exception_in_operation_start: bool,
    request_cancel: bool,
    op_definition_type: OpDefinitionType,
    caller_reference: CallerReference,
):
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[ServiceImpl()],
        workflows=[CallerWorkflow, HandlerWorkflow],
        task_queue=task_queue,
        workflow_runner=UnsandboxedWorkflowRunner(),
        workflow_failure_exception_types=[Exception],
    ):
        caller_wf_handle, handler_wf_handle = await _start_wf_and_nexus_op(
            client,
            task_queue,
            exception_in_operation_start,
            request_cancel,
            op_definition_type,
            caller_reference,
        )
        if exception_in_operation_start:
            with pytest.raises(WorkflowFailureError) as ei:
                await caller_wf_handle.result()
            e = ei.value
            assert isinstance(e, WorkflowFailureError)
            assert isinstance(e.__cause__, NexusOperationError)
            assert isinstance(e.__cause__.__cause__, NexusHandlerError)
            # ID of first command after update accepted
            assert e.__cause__.scheduled_event_id == 6
            assert e.__cause__.endpoint == make_nexus_endpoint_name(task_queue)
            assert e.__cause__.service == "ServiceInterface"
            assert (
                e.__cause__.operation == "async_operation"
                if op_definition_type == OpDefinitionType.SHORTHAND
                else "sync_or_async_operation"
            )
            return

        # TODO(dan): race here? How do we know it hasn't been canceled already?
        handler_wf_info = await handler_wf_handle.describe()
        assert handler_wf_info.status in [
            WorkflowExecutionStatus.RUNNING,
            WorkflowExecutionStatus.COMPLETED,
        ]
        await assert_caller_workflow_has_link_to_handler_workflow(
            caller_wf_handle, handler_wf_handle, handler_wf_info.run_id
        )
        await assert_handler_workflow_has_link_to_caller_workflow(
            caller_wf_handle, handler_wf_handle
        )

        if request_cancel:
            # The operation response was asynchronous and so request_cancel is honored. See
            # explanation below.
            with pytest.raises(WorkflowFailureError) as ei:
                await caller_wf_handle.result()
            e = ei.value
            assert isinstance(e, WorkflowFailureError)
            assert isinstance(e.__cause__, NexusOperationError)
            assert isinstance(e.__cause__.__cause__, CancelledError)
            # ID of first command after update accepted
            assert e.__cause__.scheduled_event_id == 6
            assert e.__cause__.endpoint == make_nexus_endpoint_name(task_queue)
            assert e.__cause__.service == "ServiceInterface"
            assert (
                e.__cause__.operation == "async_operation"
                if op_definition_type == OpDefinitionType.SHORTHAND
                else "sync_or_async_operation"
            )
            assert WorkflowOperationToken.decode(
                e.__cause__.operation_token
            ) == WorkflowOperationToken(
                namespace=handler_wf_handle._client.namespace,
                workflow_id=handler_wf_handle.id,
            )
            # Check that the handler workflow was canceled
            handler_wf_info = await handler_wf_handle.describe()
            assert handler_wf_info.status == WorkflowExecutionStatus.CANCELED
        else:
            handler_wf_info = await handler_wf_handle.describe()
            assert handler_wf_info.status == WorkflowExecutionStatus.COMPLETED
            result = await caller_wf_handle.result()
            assert result.op_output.value == "workflow result"


async def _start_wf_and_nexus_op(
    client: Client,
    task_queue: str,
    exception_in_operation_start: bool,
    request_cancel: bool,
    op_definition_type: OpDefinitionType,
    caller_reference: CallerReference,
) -> tuple[
    WorkflowHandle[CallerWorkflow, CallerWfOutput],
    WorkflowHandle[HandlerWorkflow, HandlerWfOutput],
]:
    """
    Start the caller workflow and wait until the Nexus operation has started.
    """
    await create_nexus_endpoint(task_queue, client)
    operation_workflow_id = str(uuid.uuid4())

    # Start the caller workflow and wait until it confirms the Nexus operation has started.
    block_forever_waiting_for_cancellation = request_cancel
    start_op = WithStartWorkflowOperation(
        CallerWorkflow.run,
        args=[
            CallerWfInput(
                op_input=OpInput(
                    response_type=AsyncResponse(
                        operation_workflow_id,
                        block_forever_waiting_for_cancellation,
                        op_definition_type,
                        exception_in_operation_start=exception_in_operation_start,
                    ),
                    headers={"header-key": "header-value"},
                    caller_reference=caller_reference,
                ),
            ),
            request_cancel,
            task_queue,
        ],
        id=str(uuid.uuid4()),
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
    )

    await client.execute_update_with_start_workflow(
        CallerWorkflow.wait_nexus_operation_started,
        start_workflow_operation=start_op,
    )
    caller_wf_handle = await start_op.workflow_handle()

    # check that the operation-backing workflow now exists, and that (a) the handler
    # workflow accepted the link to the calling Nexus event, and that (b) the caller
    # workflow NexusOperationStarted event received in return a link to the
    # operation-backing workflow.
    handler_wf_handle: WorkflowHandle[HandlerWorkflow, HandlerWfOutput] = (
        client.get_workflow_handle(operation_workflow_id)
    )
    return caller_wf_handle, handler_wf_handle


@pytest.mark.parametrize("exception_in_operation_start", [False, True])
@pytest.mark.parametrize(
    "op_definition_type", [OpDefinitionType.SHORTHAND, OpDefinitionType.LONGHAND]
)
@pytest.mark.parametrize(
    "caller_reference",
    [CallerReference.IMPL_WITH_INTERFACE, CallerReference.INTERFACE],
)
@pytest.mark.parametrize("response_type", [SyncResponse, AsyncResponse])
async def test_untyped_caller(
    client: Client,
    exception_in_operation_start: bool,
    op_definition_type: OpDefinitionType,
    caller_reference: CallerReference,
    response_type: ResponseType,
):
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        workflows=[UntypedCallerWorkflow, HandlerWorkflow],
        nexus_service_handlers=[ServiceImpl()],
        task_queue=task_queue,
        workflow_runner=UnsandboxedWorkflowRunner(),
        workflow_failure_exception_types=[Exception],
    ):
        if response_type == SyncResponse:
            response_type = SyncResponse(
                op_definition_type=op_definition_type,
                use_async_def=True,
                exception_in_operation_start=exception_in_operation_start,
            )
        else:
            response_type = AsyncResponse(
                operation_workflow_id=str(uuid.uuid4()),
                block_forever_waiting_for_cancellation=False,
                op_definition_type=op_definition_type,
                exception_in_operation_start=exception_in_operation_start,
            )
        await create_nexus_endpoint(task_queue, client)
        caller_wf_handle = await client.start_workflow(
            UntypedCallerWorkflow.run,
            args=[
                CallerWfInput(
                    op_input=OpInput(
                        response_type=response_type,
                        headers={},
                        caller_reference=caller_reference,
                    ),
                ),
                False,
                task_queue,
            ],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        if exception_in_operation_start:
            with pytest.raises(WorkflowFailureError) as ei:
                await caller_wf_handle.result()
            e = ei.value
            assert isinstance(e, WorkflowFailureError)
            assert isinstance(e.__cause__, NexusOperationError)
            assert isinstance(e.__cause__.__cause__, NexusHandlerError)
        else:
            result = await caller_wf_handle.result()
            assert result.op_output.value == (
                "sync response"
                if isinstance(response_type, SyncResponse)
                else "workflow result"
            )


#
# Test routing of workflow calls
#


@dataclass
class ServiceClassNameOutput:
    name: str


# TODO(dan): test interface op types not matching
# TODO(dan): async and non-async cancel methods


@nexusrpc.service
class ServiceInterfaceWithoutNameOverride:
    op: nexusrpc.Operation[None, ServiceClassNameOutput]


@nexusrpc.service(name="service-interface-🌈")
class ServiceInterfaceWithNameOverride:
    op: nexusrpc.Operation[None, ServiceClassNameOutput]


@nexusrpc.handler.service_handler
class ServiceImplInterfaceWithNeitherInterfaceNorNameOverride:
    @nexusrpc.handler.operation_handler
    def op(
        self,
    ) -> nexusrpc.handler.OperationHandler[None, ServiceClassNameOutput]:
        async def start(
            ctx: StartOperationContext, input: None
        ) -> ServiceClassNameOutput:
            return ServiceClassNameOutput(self.__class__.__name__)

        return nexusrpc.handler.SyncOperationHandler(start)


@nexusrpc.handler.service_handler(service=ServiceInterfaceWithoutNameOverride)
class ServiceImplInterfaceWithoutNameOverride:
    @nexusrpc.handler.operation_handler
    def op(
        self,
    ) -> nexusrpc.handler.OperationHandler[None, ServiceClassNameOutput]:
        async def start(
            ctx: StartOperationContext, input: None
        ) -> ServiceClassNameOutput:
            return ServiceClassNameOutput(self.__class__.__name__)

        return nexusrpc.handler.SyncOperationHandler(start)


@nexusrpc.handler.service_handler(service=ServiceInterfaceWithNameOverride)
class ServiceImplInterfaceWithNameOverride:
    @nexusrpc.handler.operation_handler
    def op(
        self,
    ) -> nexusrpc.handler.OperationHandler[None, ServiceClassNameOutput]:
        async def start(
            ctx: StartOperationContext, input: None
        ) -> ServiceClassNameOutput:
            return ServiceClassNameOutput(self.__class__.__name__)

        return nexusrpc.handler.SyncOperationHandler(start)


@nexusrpc.handler.service_handler(name="service-impl-🌈")
class ServiceImplWithNameOverride:
    @nexusrpc.handler.operation_handler
    def op(
        self,
    ) -> nexusrpc.handler.OperationHandler[None, ServiceClassNameOutput]:
        async def start(
            ctx: StartOperationContext, input: None
        ) -> ServiceClassNameOutput:
            return ServiceClassNameOutput(self.__class__.__name__)

        return nexusrpc.handler.SyncOperationHandler(start)


class NameOverride(IntEnum):
    NO = 0
    YES = 1


@workflow.defn
class ServiceInterfaceAndImplCallerWorkflow:
    @workflow.run
    async def run(
        self,
        caller_reference: CallerReference,
        name_override: NameOverride,
        task_queue: str,
    ) -> ServiceClassNameOutput:
        C, N = CallerReference, NameOverride
        if (caller_reference, name_override) == (C.INTERFACE, N.YES):
            service_cls = ServiceInterfaceWithNameOverride
        elif (caller_reference, name_override) == (C.INTERFACE, N.NO):
            service_cls = ServiceInterfaceWithoutNameOverride
        elif (caller_reference, name_override) == (C.IMPL_WITH_INTERFACE, N.YES):
            service_cls = ServiceImplWithNameOverride
        elif (caller_reference, name_override) == (C.IMPL_WITH_INTERFACE, N.NO):
            service_cls = ServiceImplInterfaceWithoutNameOverride
        elif (caller_reference, name_override) == (C.IMPL_WITHOUT_INTERFACE, N.NO):
            service_cls = ServiceImplInterfaceWithNameOverride
            service_cls = ServiceImplInterfaceWithNeitherInterfaceNorNameOverride
        else:
            raise ValueError(
                f"Invalid combination of caller_reference ({caller_reference}) and name_override ({name_override})"
            )

        nexus_client = workflow.NexusClient(
            service=service_cls,
            endpoint=make_nexus_endpoint_name(task_queue),
        )

        # TODO(dan): maybe not surprising that this doesn't type check given complexity of
        # the union?
        return await nexus_client.execute_operation(service_cls.op, None)  # type: ignore


# TODO(dan): check missing decorator behavior


async def test_service_interface_and_implementation_names(client: Client):
    # Note that:
    # - The caller can specify the service & operation via a reference to either the
    #   interface or implementation class.
    # - An interface class may optionally override its name.
    # - An implementation class may either override its name or specify an interface that
    #   it is implementing, but not both.
    # - On registering a service implementation with a worker, the name by which the
    #   service is addressed in requests is the interface name if the implementation
    #   supplies one, or else the name override made by the impl class, or else the impl
    #   class name.
    #
    # This test checks that the request is routed to the expected service under a variety
    # of scenarios related to the above considerations.
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        nexus_service_handlers=[
            ServiceImplWithNameOverride(),
            ServiceImplInterfaceWithNameOverride(),
            ServiceImplInterfaceWithoutNameOverride(),
            ServiceImplInterfaceWithNeitherInterfaceNorNameOverride(),
        ],
        workflows=[ServiceInterfaceAndImplCallerWorkflow],
        task_queue=task_queue,
        workflow_runner=UnsandboxedWorkflowRunner(),
        workflow_failure_exception_types=[Exception],
    ):
        await create_nexus_endpoint(task_queue, client)
        assert await client.execute_workflow(
            ServiceInterfaceAndImplCallerWorkflow.run,
            args=(CallerReference.INTERFACE, NameOverride.YES, task_queue),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        ) == ServiceClassNameOutput("ServiceImplInterfaceWithNameOverride")
        assert await client.execute_workflow(
            ServiceInterfaceAndImplCallerWorkflow.run,
            args=(CallerReference.INTERFACE, NameOverride.NO, task_queue),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        ) == ServiceClassNameOutput("ServiceImplInterfaceWithoutNameOverride")
        assert await client.execute_workflow(
            ServiceInterfaceAndImplCallerWorkflow.run,
            args=(
                CallerReference.IMPL_WITH_INTERFACE,
                NameOverride.YES,
                task_queue,
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        ) == ServiceClassNameOutput("ServiceImplWithNameOverride")
        assert await client.execute_workflow(
            ServiceInterfaceAndImplCallerWorkflow.run,
            args=(
                CallerReference.IMPL_WITH_INTERFACE,
                NameOverride.NO,
                task_queue,
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        ) == ServiceClassNameOutput("ServiceImplInterfaceWithoutNameOverride")
        assert await client.execute_workflow(
            ServiceInterfaceAndImplCallerWorkflow.run,
            args=(
                CallerReference.IMPL_WITHOUT_INTERFACE,
                NameOverride.NO,
                task_queue,
            ),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        ) == ServiceClassNameOutput(
            "ServiceImplInterfaceWithNeitherInterfaceNorNameOverride"
        )


@nexusrpc.service
class ServiceWithOperationsThatExecuteWorkflowBeforeStartingBackingWorkflow:
    my_workflow_run_operation: nexusrpc.Operation[None, None]
    my_manual_async_operation: nexusrpc.Operation[None, None]


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, input: str) -> str:
        return input


@nexusrpc.handler.service_handler
class ServiceImplWithOperationsThatExecuteWorkflowBeforeStartingBackingWorkflow:
    @nexusrpc.handler.operation_handler
    def my_workflow_run_operation(
        self,
    ) -> nexusrpc.handler.OperationHandler[None, str]:
        async def start(
            ctx: StartOperationContext, input: None
        ) -> WorkflowOperationToken[str]:
            tctx = TemporalOperationContext.current()
            result_1 = await tctx.client.execute_workflow(
                EchoWorkflow.run,
                "result-1",
                id=str(uuid.uuid4()),
                task_queue=tctx.task_queue,
            )
            # In case result_1 is incorrectly being delivered to the caller as the operation
            # result, give time for that incorrect behavior to occur.
            await asyncio.sleep(0.5)
            return await tctx.start_workflow(
                EchoWorkflow.run,
                f"{result_1}-result-2",
                id=str(uuid.uuid4()),
            )

        return temporalio.nexus.handler.WorkflowRunOperationHandler(start)


@workflow.defn
class WorkflowCallingNexusOperationThatExecutesWorkflowBeforeStartingBackingWorkflow:
    @workflow.run
    async def run(self, input: str, task_queue: str) -> str:
        nexus_client = workflow.NexusClient(
            service=ServiceImplWithOperationsThatExecuteWorkflowBeforeStartingBackingWorkflow,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        return await nexus_client.execute_operation(
            ServiceImplWithOperationsThatExecuteWorkflowBeforeStartingBackingWorkflow.my_workflow_run_operation,
            None,
        )


async def test_workflow_run_operation_can_execute_workflow_before_starting_backing_workflow(
    client: Client,
):
    task_queue = str(uuid.uuid4())
    async with Worker(
        client,
        workflows=[
            EchoWorkflow,
            WorkflowCallingNexusOperationThatExecutesWorkflowBeforeStartingBackingWorkflow,
        ],
        nexus_service_handlers=[
            ServiceImplWithOperationsThatExecuteWorkflowBeforeStartingBackingWorkflow(),
        ],
        task_queue=task_queue,
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await create_nexus_endpoint(task_queue, client)
        result = await client.execute_workflow(
            WorkflowCallingNexusOperationThatExecutesWorkflowBeforeStartingBackingWorkflow.run,
            args=("result-1", task_queue),
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        assert result == "result-1-result-2"


# TODO(dan): test invalid service interface implementations
# TODO(dan): test caller passing output_type


async def assert_caller_workflow_has_link_to_handler_workflow(
    caller_wf_handle: WorkflowHandle,
    handler_wf_handle: WorkflowHandle,
    handler_wf_run_id: str,
):
    caller_history = await caller_wf_handle.fetch_history()
    op_started_event = next(
        e
        for e in caller_history.events
        if (
            e.event_type
            == temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_STARTED
        )
    )
    if not len(op_started_event.links) == 1:
        pytest.fail(
            f"Expected 1 link on NexusOperationStarted event, got {len(op_started_event.links)}"
        )
    [link] = op_started_event.links
    assert link.workflow_event.namespace == handler_wf_handle._client.namespace
    assert link.workflow_event.workflow_id == handler_wf_handle.id
    assert link.workflow_event.run_id
    assert link.workflow_event.run_id == handler_wf_run_id
    assert (
        link.workflow_event.event_ref.event_type
        == temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED
    )


async def assert_handler_workflow_has_link_to_caller_workflow(
    caller_wf_handle: WorkflowHandle,
    handler_wf_handle: WorkflowHandle,
):
    handler_history = await handler_wf_handle.fetch_history()
    wf_started_event = next(
        e
        for e in handler_history.events
        if (
            e.event_type
            == temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED
        )
    )
    if not len(wf_started_event.links) == 1:
        pytest.fail(
            f"Expected 1 link on WorkflowExecutionStarted event, got {len(wf_started_event.links)}"
        )
    [link] = wf_started_event.links
    assert link.workflow_event.namespace == caller_wf_handle._client.namespace
    assert link.workflow_event.workflow_id == caller_wf_handle.id
    assert link.workflow_event.run_id
    assert link.workflow_event.run_id == caller_wf_handle.first_execution_run_id
    assert (
        link.workflow_event.event_ref.event_type
        == temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED
    )


# When request_cancel is True, the NexusOperationHandle in the workflow evolves
# through the following states:
#                           start_fut             result_fut            handle_task w/ fut_waiter                        (task._must_cancel)
#
# Case 1: Sync Nexus operation response w/ cancellation of NexusOperationHandle
# -----------------------------------------------------------------------------
# >>>>>>>>>>>> WFT 1
# after await start       : Future_7856[FINISHED] Future_7984[FINISHED] Task[PENDING] fut_waiter = Future_8240[FINISHED]) (False)
# before op_handle.cancel : Future_7856[FINISHED] Future_7984[FINISHED] Task[PENDING] fut_waiter = Future_8240[FINISHED]) (False)
# Future_8240[FINISHED].cancel() -> False  # no state transition; fut_waiter is already finished
# cancel returned         : True
# before await op_handle  : Future_7856[FINISHED] Future_7984[FINISHED] Task[PENDING] fut_waiter = Future_8240[FINISHED]) (True)
# --> Despite cancel having been requested, this await on the nexus op handle does not
#     raise CancelledError, because the task's underlying fut_waiter is already finished.
# after await op_handle   : Future_7856[FINISHED] Future_7984[FINISHED] Task[FINISHED] fut_waiter = None) (False)
#
#
# Case 2: Async Nexus operation response w/ cancellation of NexusOperationHandle
# ------------------------------------------------------------------------------
# >>>>>>>>>>>> WFT 1
# after await start       : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[PENDING]) (False)
# >>>>>>>>>>>> WFT 2
# >>>>>>>>>>>> WFT 3
# after await proceed     : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[PENDING]) (False)
# before op_handle.cancel : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[PENDING]) (False)
# Future_7952[PENDING].cancel() -> True  # transition to cancelled state; fut_waiter was not finished
# cancel returned         : True
# before await op_handle  : Future_7568[FINISHED] Future_7696[PENDING] Task[PENDING] fut_waiter = Future_7952[CANCELLED]) (False)
# --> This await on the nexus op handle raises CancelledError, because the task's underlying fut_waiter is cancelled.
#
# Thus in the sync case, although the caller workflow attempted to cancel the
# NexusOperationHandle, this did not result in a CancelledError when the handle was
# awaited, because both resolve_nexus_operation_start and resolve_nexus_operation jobs
# were sent in the same activation and hence the task's fut_waiter was already finished.
#
# But in the async case, at the time that we cancel the NexusOperationHandle, only the
# resolve_nexus_operation_start job had been sent; the result_fut was unresolved. Thus
# when the handle was awaited, CancelledError was raised.
#
# To create output like that above, set the following __repr__s:
# asyncio.Future:
# def __repr__(self):
#     return f"{self.__class__.__name__}_{str(id(self))[-4:]}[{self._state}]"
# _NexusOperationHandle:
# def __repr__(self) -> str:
#     return (
#         f"{self._start_fut} "
#         f"{self._result_fut} "
#         f"Task[{self._task._state}] fut_waiter = {self._task._fut_waiter}) ({self._task._must_cancel})"
#     )
