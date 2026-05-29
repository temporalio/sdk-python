"""
This file exists to test for type-checker false positives and false negatives.
It doesn't contain any test functions.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypeAlias
from unittest.mock import Mock

import nexusrpc

import temporalio.nexus
from temporalio import activity, workflow
from temporalio.client import Client, NexusOperationHandle
from temporalio.nexus import TemporalNexusOperationStartHandlerFunc
from temporalio.service import ServiceClient


@dataclass
class MyInput:
    pass


@dataclass
class MyOutput:
    pass


@workflow.defn
class MyNoArgProcWorkflow:
    @workflow.run
    async def run(self) -> None:
        pass


@workflow.defn
class MyOneArgProcWorkflow:
    @workflow.run
    async def run(self, _input: MyInput) -> None:
        pass


@workflow.defn
class MyTwoArgProcWorkflow:
    @workflow.run
    async def run(self, _input: MyInput, _arg2: int) -> None:
        pass


@workflow.defn
class MyThreeArgProcWorkflow:
    @workflow.run
    async def run(self, _input: MyInput, _arg2: int, _arg3: int) -> None:
        pass


@workflow.defn
class MyFourArgProcWorkflow:
    @workflow.run
    async def run(self, _input: MyInput, _arg2: int, _arg3: int, _arg4: int) -> None:
        pass


@workflow.defn
class MyFiveArgProcWorkflow:
    @workflow.run
    async def run(
        self, _input: MyInput, _arg2: int, _arg3: int, _arg4: int, _arg5: int
    ) -> None:
        pass


@activity.defn
async def my_no_arg_activity() -> None:
    pass


@activity.defn
async def my_one_arg_activity(_input: MyInput) -> None:
    pass


@activity.defn
async def my_two_arg_activity(_input: MyInput, _arg2: int) -> None:
    pass


@activity.defn
async def my_three_arg_activity(_input: MyInput, _arg2: int, _arg3: int) -> None:
    pass


@activity.defn
async def my_four_arg_activity(
    _input: MyInput, _arg2: int, _arg3: int, _arg4: int
) -> None:
    pass


@activity.defn
async def my_five_arg_activity(
    _input: MyInput, _arg2: int, _arg3: int, _arg4: int, _arg5: int
) -> None:
    pass


@nexusrpc.service
class MyService:
    my_sync_operation: nexusrpc.Operation[MyInput, MyOutput]
    my_workflow_run_operation: nexusrpc.Operation[MyInput, MyOutput]
    my_temporal_operation: nexusrpc.Operation[int, None]


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

    @temporalio.nexus.temporal_operation
    async def my_temporal_operation(
        self,
        _ctx: temporalio.nexus.TemporalNexusStartOperationContext,
        client: temporalio.nexus.TemporalNexusClient,
        input: int,
    ) -> temporalio.nexus.TemporalOperationResult[None]:
        """
        Typed proc workflow and activity starts from a generic Temporal Nexus
        operation handler infer TemporalOperationResult[None] for 0 to 5
        workflow or activity parameters.
        """
        if input == 0:
            result_0: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_workflow(MyNoArgProcWorkflow.run, id="proc-0")
            return result_0
        if input == 1:
            result_1: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_workflow(
                MyOneArgProcWorkflow.run, MyInput(), id="proc-1"
            )
            return result_1
        if input == 2:
            result_2: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_workflow(
                MyTwoArgProcWorkflow.run, args=[MyInput(), 2], id="proc-2"
            )
            return result_2
        if input == 3:
            result_3: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_workflow(
                MyThreeArgProcWorkflow.run,
                args=[MyInput(), 2, 3],
                id="proc-3",
            )
            return result_3
        if input == 4:
            result_4: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_workflow(
                MyFourArgProcWorkflow.run,
                args=[MyInput(), 2, 3, 4],
                id="proc-4",
            )
            return result_4
        if input == 5:
            result_5: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_workflow(
                MyFiveArgProcWorkflow.run,
                args=[MyInput(), 2, 3, 4, 5],
                id="proc-5",
            )
            return result_5

        # Typed activity starts infer TemporalOperationResult[None] for 0 to 5
        # activity parameters. Activities require a start_to_close_timeout.
        if input == 6:
            activity_result_0: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_activity(
                my_no_arg_activity,
                id="activity-0",
                start_to_close_timeout=timedelta(seconds=5),
            )
            return activity_result_0
        if input == 7:
            activity_result_1: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_activity(
                my_one_arg_activity,
                MyInput(),
                id="activity-1",
                start_to_close_timeout=timedelta(seconds=5),
            )
            return activity_result_1
        if input == 8:
            activity_result_2: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_activity(
                my_two_arg_activity,
                args=[MyInput(), 2],
                id="activity-2",
                start_to_close_timeout=timedelta(seconds=5),
            )
            return activity_result_2
        if input == 9:
            activity_result_3: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_activity(
                my_three_arg_activity,
                args=[MyInput(), 2, 3],
                id="activity-3",
                start_to_close_timeout=timedelta(seconds=5),
            )
            return activity_result_3
        if input == 10:
            activity_result_4: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_activity(
                my_four_arg_activity,
                args=[MyInput(), 2, 3, 4],
                id="activity-4",
                start_to_close_timeout=timedelta(seconds=5),
            )
            return activity_result_4
        if input == 11:
            activity_result_5: temporalio.nexus.TemporalOperationResult[
                None
            ] = await client.start_activity(
                my_five_arg_activity,
                args=[MyInput(), 2, 3, 4, 5],
                id="activity-5",
                start_to_close_timeout=timedelta(seconds=5),
            )
            return activity_result_5
        if input == 12:
            # omitting the required start_to_close_timeout is a type error
            # assert-type-error-pyright: 'No overloads for "start_activity" match'
            return await client.start_activity(  # type: ignore
                my_no_arg_activity,
                id="activity-missing-timeout",
            )
        if input == 13:
            # assert-type-error-pyright: 'No overloads for "start_activity" match'
            return await client.start_activity(  # type: ignore
                my_one_arg_activity,
                # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter'
                "wrong-input-type",  # type: ignore
                id="activity-wrong-input",
                start_to_close_timeout=timedelta(seconds=5),
            )

        # assert-type-error-pyright: 'No overloads for "start_workflow" match'
        return await client.start_workflow(  # type: ignore
            MyOneArgProcWorkflow.run,
            # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter'
            "wrong-input-type",  # type: ignore
            id="proc-wrong-input",
        )


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

    @temporalio.nexus.temporal_operation
    async def my_temporal_operation(
        self,
        _ctx: temporalio.nexus.TemporalNexusStartOperationContext,
        _client: temporalio.nexus.TemporalNexusClient,
        _input: int,
    ) -> temporalio.nexus.TemporalOperationResult[None]:
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

    @temporalio.nexus.temporal_operation
    async def my_temporal_operation(
        self,
        _ctx: temporalio.nexus.TemporalNexusStartOperationContext,
        _client: temporalio.nexus.TemporalNexusClient,
        _input: int,
    ) -> temporalio.nexus.TemporalOperationResult[None]:
        raise NotImplementedError


_handler: TemporalNexusOperationStartHandlerFunc[
    MyServiceHandler,
    int,
    None,
] = MyServiceHandler.my_temporal_operation

_BadHandler: TypeAlias = temporalio.nexus.TemporalNexusOperationStartHandlerFunc[
    MyServiceHandler,
    str,
    None,
]

_bad_handler: TemporalNexusOperationStartHandlerFunc[
    MyServiceHandler,
    str,
    None,
    # assert-type-error-pyright: 'is not assignable to declared type'
] = MyServiceHandler.my_temporal_operation  # type: ignore


class MyUnsafeContextAnnotationServiceHandler:
    # A temporal operation receives TemporalStartOperationContext at runtime, so
    # requiring an arbitrary user subclass is not safe.
    class MyCustomTemporalStartOperationContext(
        temporalio.nexus.TemporalNexusStartOperationContext
    ):
        def custom_state(self) -> str:
            raise NotImplementedError

    # assert-type-error-pyright: 'cannot be assigned to parameter "start".+temporal_operation'
    @temporalio.nexus.temporal_operation  # type: ignore
    async def my_temporal_operation_with_workflow_run_context(
        self,
        _ctx: MyCustomTemporalStartOperationContext,
        _client: temporalio.nexus.TemporalNexusClient,
        _input: int,
    ) -> temporalio.nexus.TemporalOperationResult[None]:
        raise NotImplementedError

    # A workflow run operation receives WorkflowRunOperationContext at runtime,
    # so requiring an arbitrary user subclass is not safe.
    class MyCustomWorkflowRunOperationContext(
        temporalio.nexus.WorkflowRunOperationContext
    ):
        def custom_state(self) -> str:
            raise NotImplementedError

    # assert-type-error-pyright: 'cannot be assigned to parameter "start".+workflow_run_operation'
    @temporalio.nexus.workflow_run_operation  # type: ignore
    async def my_workflow_run_operation_with_custom_context(
        self,
        _ctx: MyCustomWorkflowRunOperationContext,
        _input: MyInput,
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

        # temporal operation
        _output_3: None = await nexus_client.execute_operation(  # type: ignore
            MyService.my_temporal_operation, 0
        )
        _handle_3: workflow.NexusOperationHandle[
            None
        ] = await nexus_client.start_operation(MyService.my_temporal_operation, 0)
        _output_3_1: None = await _handle_3  # type: ignore


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

        # temporal operation
        _output_3: None = await nexus_client.execute_operation(  # type: ignore
            MyServiceHandler.my_temporal_operation, 0
        )
        _handle_3: workflow.NexusOperationHandle[
            None
        ] = await nexus_client.start_operation(
            MyServiceHandler.my_temporal_operation, 0
        )
        _output_3_1: None = await _handle_3  # type: ignore


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
        # assert-type-error-pyright: 'No overloads for "execute_operation" match'
        await nexus_client.execute_operation(  # type: ignore
            MyService.my_temporal_operation,
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
        # assert-type-error-pyright: 'No overloads for "execute_operation" match'
        await nexus_client.execute_operation(  # type: ignore
            MyServiceHandler.my_temporal_operation,  # type: ignore[arg-type]
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

        # assert-type-error-pyright: 'No overloads for "execute_operation" match'
        await nexus_client.execute_operation(  # type: ignore
            # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "operation"'
            MyServiceHandler2.my_temporal_operation,  # type: ignore
            0,
        )


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
    handler_nexus_client = client.create_nexus_client(
        MyServiceHandler,
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

    # result_type is not allowed when an operation is provided
    await nexus_client.execute_operation(
        # assert-type-error-pyright: 'cannot be assigned to parameter "operation" of type "str"'
        MyService.my_sync_operation,  # type: ignore
        MyInput(),
        id="op-1",
        result_type=str,
    )

    # string operation name and result_type infers output type
    _str_op_result_type_output: MyOutput = await nexus_client.execute_operation(
        "my_sync_operation", MyInput(), id="op-1", result_type=MyOutput
    )

    # execute with workflow run handler infers output type
    _workflow_run_output: MyOutput = await handler_nexus_client.execute_operation(
        MyServiceHandler.my_workflow_run_operation,
        MyInput(),
        id="op-1",
    )

    # execute with temporal operation handler infers output type
    _temporal_output: None = await handler_nexus_client.execute_operation(  # type: ignore[func-returns-value]
        MyServiceHandler.my_temporal_operation,
        0,
        id="op-1",
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

    # result_type is not allowed when an operation is provided
    await nexus_client.start_operation(
        # assert-type-error-pyright: 'cannot be assigned to parameter "operation" of type "str"'
        MyServiceHandler.my_sync_operation,  # type: ignore
        MyInput(),
        id="op-1",
        result_type=str,
    )

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

    # starting with workflow run handler infers output type on the handle
    # and result from the handle
    _workflow_run_handle: NexusOperationHandle[
        MyOutput
    ] = await handler_nexus_client.start_operation(
        MyServiceHandler.my_workflow_run_operation,
        MyInput(),
        id="op-1",
    )

    # starting with temporal operation handler infers output type on the handle
    # and result from the handle
    _workflow_run_handle_output: MyOutput = await _workflow_run_handle.result()
    _temporal_handle: NexusOperationHandle[
        None
    ] = await handler_nexus_client.start_operation(
        MyServiceHandler.my_temporal_operation,
        0,
        id="op-1",
    )
    _temporal_handle_output: None = await _temporal_handle.result()  # type: ignore[func-returns-value]

    # workflow run and temporal operation handlers reject wrong input types
    # assert-type-error-pyright: 'No overloads for "execute_operation" match'
    await handler_nexus_client.execute_operation(  # type: ignore
        MyServiceHandler.my_workflow_run_operation,  # type: ignore[arg-type]
        # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "arg"'
        "wrong-input-type",  # type: ignore
        id="op-1",
    )
    # assert-type-error-pyright: 'No overloads for "start_operation" match'
    await handler_nexus_client.start_operation(  # type: ignore
        MyServiceHandler.my_workflow_run_operation,  # type: ignore[arg-type]
        # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "arg"'
        "wrong-input-type",  # type: ignore
        id="op-1",
    )
    # assert-type-error-pyright: 'No overloads for "execute_operation" match'
    await handler_nexus_client.execute_operation(  # type: ignore
        MyServiceHandler.my_temporal_operation,  # type: ignore[arg-type]
        # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "arg"'
        "wrong-input-type",  # type: ignore
        id="op-1",
    )
    # assert-type-error-pyright: 'No overloads for "start_operation" match'
    await handler_nexus_client.start_operation(  # type: ignore
        MyServiceHandler.my_temporal_operation,  # type: ignore[arg-type]
        # assert-type-error-pyright: 'Argument of type .+ cannot be assigned to parameter "arg"'
        "wrong-input-type",  # type: ignore
        id="op-1",
    )

    # getting a handle with a string produces a handle to Any
    _str_op_handle: NexusOperationHandle[Any] = client.get_nexus_operation_handle(
        "op-1"
    )

    # getting a handle with an explicit type produces handle of that type
    _result_type_get_handle: NexusOperationHandle[MyOutput] = (
        client.get_nexus_operation_handle("op-1", result_type=MyOutput)
    )

    # getting a handle with an operation definition produces a handle of the operation
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

    # mismatched types on get_nexus_operation_handle produce a type error
    # assert-type-error-pyright: 'Type "NexusOperationHandle\[str\]" is not assignable to declared type "NexusOperationHandle\[MyOutput\]"'
    _mismatch_handle: NexusOperationHandle[MyOutput] = (
        client.get_nexus_operation_handle(  # type: ignore
            "op-1",
            result_type=str,  # type: ignore
        )
    )

    # functions with invalid signatures produce a type error
    class InvalidServiceHandler:
        async def invalid(self, _ctx: str, _input: str) -> str:
            raise NotImplementedError()

    # assert-type-error-pyright: 'No overloads for "start_operation" match'
    _invalid_handle: NexusOperationHandle[str] = await nexus_client.start_operation(
        InvalidServiceHandler.invalid,  # type: ignore
        "foo",
        id="invalid",
    )

    # assert-type-error-pyright: 'No overloads for "execute_operation" match'
    _invalid_result: str = await nexus_client.execute_operation(
        InvalidServiceHandler.invalid,  # type: ignore
        "foo",
        id="invalid",
    )
