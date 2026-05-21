"""
This file exists to test for type-checker false positives and false negatives.
It doesn't contain any test functions.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from unittest.mock import Mock

import nexusrpc

import temporalio.nexus
from temporalio import workflow
from temporalio.client import Client, NexusOperationHandle
from temporalio.service import ServiceClient


@dataclass
class MyInput:
    pass


@dataclass
class MyOutput:
    pass


@nexusrpc.service
class MyService:
    my_sync_operation: nexusrpc.Operation[MyInput, MyOutput]
    my_workflow_run_operation: nexusrpc.Operation[MyInput, MyOutput]


@nexusrpc.service
class MyNoInputService:
    my_no_input_operation: nexusrpc.Operation[None, MyOutput]


@nexusrpc.handler.service_handler(service=MyService)
class MyServiceHandler:
    @nexusrpc.handler.sync_operation
    async def my_sync_operation(
        self, _ctx: nexusrpc.handler.StartOperationContext, _input: MyInput
    ) -> MyOutput:
        raise NotImplementedError

    @temporalio.nexus.workflow_run_operation
    async def my_workflow_run_operation(
        self, _ctx: temporalio.nexus.WorkflowRunOperationContext, _input: MyInput
    ) -> temporalio.nexus.WorkflowHandle[MyOutput]:
        raise NotImplementedError


@nexusrpc.handler.service_handler(service=MyService)
class MyServiceHandler2:
    @nexusrpc.handler.sync_operation
    async def my_sync_operation(
        self, _ctx: nexusrpc.handler.StartOperationContext, _input: MyInput
    ) -> MyOutput:
        raise NotImplementedError

    @temporalio.nexus.workflow_run_operation
    async def my_workflow_run_operation(
        self, _ctx: temporalio.nexus.WorkflowRunOperationContext, _input: MyInput
    ) -> temporalio.nexus.WorkflowHandle[MyOutput]:
        raise NotImplementedError


@nexusrpc.handler.service_handler
class MyServiceHandlerWithoutServiceDefinition:
    @nexusrpc.handler.sync_operation
    async def my_sync_operation(
        self, _ctx: nexusrpc.handler.StartOperationContext, _input: MyInput
    ) -> MyOutput:
        raise NotImplementedError

    @temporalio.nexus.workflow_run_operation
    async def my_workflow_run_operation(
        self, _ctx: temporalio.nexus.WorkflowRunOperationContext, _input: MyInput
    ) -> temporalio.nexus.WorkflowHandle[MyOutput]:
        raise NotImplementedError


@workflow.defn
class MyWorkflow1:
    @workflow.run
    async def test_invoke_by_operation_definition_happy_path(self) -> None:
        """
        When a nexus client  calls an operation by referencing an operation definition on
        a service definition, the output type is inferred correctly.
        """
        nexus_client = workflow.create_nexus_client(
            service=MyService,
            endpoint="fake-endpoint",
        )
        input = MyInput()

        # sync operation
        _output_1: MyOutput = await nexus_client.execute_operation(
            MyService.my_sync_operation, input
        )
        _handle_1: workflow.NexusOperationHandle[
            MyOutput
        ] = await nexus_client.start_operation(MyService.my_sync_operation, input)
        _output_1_1: MyOutput = await _handle_1

        # workflow run operation
        _output_2: MyOutput = await nexus_client.execute_operation(
            MyService.my_workflow_run_operation, input
        )
        _handle_2: workflow.NexusOperationHandle[
            MyOutput
        ] = await nexus_client.start_operation(
            MyService.my_workflow_run_operation, input
        )
        _output_2_1: MyOutput = await _handle_2


@workflow.defn
class MyWorkflow2:
    @workflow.run
    async def test_invoke_by_operation_handler_happy_path(self) -> None:
        """
        When a nexus client calls an operation by referencing an operation handler on a
        service handler, the output type is inferred correctly.
        """
        nexus_client = workflow.create_nexus_client(
            service=MyServiceHandler,  # MyService would also work
            endpoint="fake-endpoint",
        )
        input = MyInput()

        # sync operation
        _output_1: MyOutput = await nexus_client.execute_operation(
            MyServiceHandler.my_sync_operation, input
        )
        _handle_1: workflow.NexusOperationHandle[
            MyOutput
        ] = await nexus_client.start_operation(
            MyServiceHandler.my_sync_operation, input
        )
        _output_1_1: MyOutput = await _handle_1

        # workflow run operation
        _output_2: MyOutput = await nexus_client.execute_operation(
            MyServiceHandler.my_workflow_run_operation, input
        )
        _handle_2: workflow.NexusOperationHandle[
            MyOutput
        ] = await nexus_client.start_operation(
            MyServiceHandler.my_workflow_run_operation, input
        )
        _output_2_1: MyOutput = await _handle_2


@workflow.defn
class MyWorkflow3:
    @workflow.run
    async def test_invoke_by_operation_definition_wrong_input_type(self) -> None:
        """
        When a nexus client calls an operation by referencing an operation definition on
        a service definition, there is a type error if the input type is wrong.
        """
        nexus_client = workflow.create_nexus_client(
            service=MyService,
            endpoint="fake-endpoint",
        )
        # assert-type-error-pyright: 'No overloads for "execute_operation" match'
        await nexus_client.execute_operation(  # type: ignore
            MyService.my_sync_operation,
            # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "input"'
            "wrong-input-type",  # type: ignore
        )


@workflow.defn
class MyWorkflow4:
    @workflow.run
    async def test_invoke_by_operation_handler_wrong_input_type(self) -> None:
        """
        When a nexus client calls an operation by referencing an operation handler on a
        service handler, there is a type error if the input type is wrong.
        """
        nexus_client = workflow.create_nexus_client(
            service=MyServiceHandler,
            endpoint="fake-endpoint",
        )
        # assert-type-error-pyright: 'No overloads for "execute_operation" match'
        await nexus_client.execute_operation(  # type: ignore
            MyServiceHandler.my_sync_operation,  # type: ignore[arg-type]
            # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "input"'
            "wrong-input-type",  # type: ignore
        )


@workflow.defn
class MyWorkflow5:
    @workflow.run
    async def test_invoke_by_operation_handler_method_on_wrong_service(self) -> None:
        """
        When a nexus client calls an operation by referencing an operation handler method
        on a service handler, there is a type error if the method does not belong to the
        service for which the client was created.

        (This form of type safety is not available when referencing an operation definition)
        """
        nexus_client = workflow.create_nexus_client(
            service=MyServiceHandler,
            endpoint="fake-endpoint",
        )
        # assert-type-error-pyright: 'No overloads for "execute_operation" match'
        await nexus_client.execute_operation(  # type: ignore
            # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "operation"'
            MyServiceHandler2.my_sync_operation,  # type: ignore
            MyInput(),
        )


# ── Standalone Nexus Operation type tests ──
async def standalone_operation_type_tests():
    client = Client(service_client=Mock(spec=ServiceClient))
    nexus_client = client.create_nexus_client(
        MyService,
        endpoint="fake-endpoint",
    )
    no_input_nexus_client = client.create_nexus_client(
        MyNoInputService,
        endpoint="fake-endpoint",
    )

    # execute with an operation definition infers output type
    _op_defn_output: MyOutput = await nexus_client.execute_operation(
        MyService.my_sync_operation,
        MyInput(),
        id="op-1",
        schedule_to_start_timeout=timedelta(seconds=1),
        start_to_close_timeout=timedelta(seconds=2),
    )

    # string operation name and result_type infers output type
    _str_op_result_type_output: MyOutput = await nexus_client.execute_operation(
        "my_sync_operation", MyInput(), id="op-1", result_type=MyOutput
    )

    # omitting arg for string operation names is not supported
    # assert-type-error-pyright: 'No overloads for "execute_operation" match'
    await nexus_client.execute_operation(  # type: ignore
        "my_sync_operation",
        id="op-1",
        result_type=MyOutput,
    )
    # assert-type-error-pyright: 'No overloads for "start_operation" match'
    await nexus_client.start_operation(  # type: ignore
        "my_sync_operation",
        id="op-1",
        result_type=MyOutput,
    )

    # omitting arg for callable operations is not supported
    # assert-type-error-pyright: 'No overloads for "execute_operation" match'
    await nexus_client.execute_operation(  # type: ignore
        MyServiceHandler.my_sync_operation,
        id="op-1",
        result_type=MyOutput,
    )
    # assert-type-error-pyright: 'No overloads for "start_operation" match'
    await nexus_client.start_operation(  # type: ignore
        MyServiceHandler.my_sync_operation,
        id="op-1",
        result_type=MyOutput,
    )

    # no-input operation definitions must still be called with explicit None
    _no_input_op_defn_output: MyOutput = await no_input_nexus_client.execute_operation(
        MyNoInputService.my_no_input_operation,
        None,
        id="op-1",
    )
    _no_input_op_defn_handle: NexusOperationHandle[
        MyOutput
    ] = await no_input_nexus_client.start_operation(
        MyNoInputService.my_no_input_operation,
        None,
        id="op-1",
    )
    _no_input_op_defn_handle_output: MyOutput = await _no_input_op_defn_handle.result()

    # omitting arg for no-input operation definitions is not supported
    # assert-type-error-pyright: 'No overloads for "execute_operation" match'
    await no_input_nexus_client.execute_operation(  # type: ignore
        MyNoInputService.my_no_input_operation,
        id="op-1",
    )
    # assert-type-error-pyright: 'No overloads for "start_operation" match'
    await no_input_nexus_client.start_operation(  # type: ignore
        MyNoInputService.my_no_input_operation,
        id="op-1",
    )

    # execute with an operation definition and a wrong input type produces a type error
    # assert-type-error-pyright: 'No overloads for "execute_operation" match'
    await nexus_client.execute_operation(  # type: ignore
        MyService.my_sync_operation,
        # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "arg"'
        "wrong-input-type",  # type: ignore
        id="op-1",
    )

    # start with an operation definition and a wrong input type produces a type error
    # assert-type-error-pyright: 'No overloads for "start_operation" match'
    await nexus_client.start_operation(  # type: ignore
        MyService.my_sync_operation,
        # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "arg"'
        "wrong-input-type",  # type: ignore
        id="op-1",
    )

    # starting with an operation definition infers output type on the handle and
    # result from handle
    _defn_handle: NexusOperationHandle[MyOutput] = await nexus_client.start_operation(
        MyService.my_sync_operation,
        MyInput(),
        id="op-1",
        schedule_to_start_timeout=timedelta(seconds=1),
        start_to_close_timeout=timedelta(seconds=2),
    )
    _defn_handle_output: MyOutput = await _defn_handle.result()

    # starting with string operation name and result_type infers output type on the handle
    # and result from the handle
    _str_op_result_type_handle: NexusOperationHandle[
        MyOutput
    ] = await nexus_client.start_operation(
        "my_sync_operation", MyInput(), id="op-1", result_type=MyOutput
    )
    _str_op_result_type_handle_output: MyOutput = (
        await _str_op_result_type_handle.result()
    )

    # getting a handle with a string produces a handle to Any
    _str_op_handle: NexusOperationHandle[Any] = client.get_nexus_operation_handle(
        "op-1"
    )

    # getting a handle with an explicit type produces handle of that type
    _result_type_get_handle: NexusOperationHandle[MyOutput] = (
        client.get_nexus_operation_handle("op-1", result_type=MyOutput)
    )

    # getting a handle with an operation defintion produces a handle of the operation
    # output type
    _op_defn_get_handle: NexusOperationHandle[MyOutput] = (
        client.get_nexus_operation_handle("op-1", operation=MyService.my_sync_operation)
    )

    # providing both operation and result_type to get_nexus_operation_handle
    # produces a no overload found error
    # assert-type-error-pyright: 'No overloads for "get_nexus_operation_handle" match'
    _result_type_op_defn_get_handle: NexusOperationHandle[MyOutput] = (
        client.get_nexus_operation_handle(  # type: ignore
            "op-1",
            operation=MyService.my_sync_operation,
            result_type=str,
        )
    )

    # mismatched types on get_nexus_operation_handle produces type error
    # assert-type-error-pyright: 'Type "NexusOperationHandle\[str\]" is not assignable to declared type "NexusOperationHandle\[MyOutput\]"'
    _mismatch_handle: NexusOperationHandle[MyOutput] = (
        client.get_nexus_operation_handle(  # type: ignore
            "op-1",
            result_type=str,  # type: ignore
        )
    )
