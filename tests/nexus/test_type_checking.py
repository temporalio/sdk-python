"""
This file exists to test for type-checker false positives and false negatives.
It doesn't contain any test functions.
"""

from dataclasses import dataclass

import nexusrpc

import temporalio.nexus
from temporalio import workflow


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
    async def test_invoke_by_operation_name_happy_path(self) -> None:
        """
        When a nexus client calls an operation by referencing an operation name, the
        output type is inferred as Unknown.
        """
        nexus_client = workflow.create_nexus_client(
            service=MyServiceHandler,
            endpoint="fake-endpoint",
        )
        input = MyInput()
        # TODO: mypy fails these since no type is inferred, so we're forced to add a
        # `type: ignore`. As a result this function doesn't currently prove anything, but
        # one can confirm the inferred type is Unknown in an IDE.
        _output_1 = await nexus_client.execute_operation("my_sync_operation", input)  # type: ignore[var-annotated]
        _output_2 = await nexus_client.execute_operation(  # type: ignore[var-annotated]
            "my_workflow_run_operation", input
        )


@workflow.defn
class MyWorkflow4:
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
class MyWorkflow5:
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
            MyServiceHandler.my_sync_operation,
            # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "input"'
            "wrong-input-type",  # type: ignore
        )


@workflow.defn
class MyWorkflow6:
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
