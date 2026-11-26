"""
Test that operation_handler decorator results in operation definitions with the correct name
and input/output types.
"""

import warnings
from dataclasses import dataclass
from typing import Any, ClassVar

import nexusrpc.handler
import pytest

from temporalio import nexus
from temporalio.nexus import WorkflowRunOperationContext, workflow_run_operation
from temporalio.nexus._util import get_operation_factory


@dataclass
class Input:
    pass


@dataclass
class Output:
    pass


@dataclass
class _TestCase:
    Service: ClassVar[type[Any]]
    expected_operations: ClassVar[dict[str, nexusrpc.Operation]]


class NotCalled(_TestCase):
    @nexusrpc.handler.service_handler
    class Service:
        @workflow_run_operation
        async def my_workflow_run_operation_handler(
            self, _ctx: WorkflowRunOperationContext, _input: Input
        ) -> nexus.WorkflowHandle[Output]:
            raise NotImplementedError

    expected_operations = {
        "my_workflow_run_operation_handler": nexusrpc.Operation(
            name="my_workflow_run_operation_handler",
            input_type=Input,
            output_type=Output,
        ),
    }


class CalledWithoutArgs(_TestCase):
    @nexusrpc.handler.service_handler
    class Service:
        @workflow_run_operation
        async def my_workflow_run_operation_handler(
            self, _ctx: WorkflowRunOperationContext, _input: Input
        ) -> nexus.WorkflowHandle[Output]:
            raise NotImplementedError

    expected_operations = NotCalled.expected_operations


class CalledWithNameOverride(_TestCase):
    @nexusrpc.handler.service_handler
    class Service:
        @workflow_run_operation(name="operation-name")
        async def workflow_run_operation_with_name_override(
            self, _ctx: WorkflowRunOperationContext, _input: Input
        ) -> nexus.WorkflowHandle[Output]:
            raise NotImplementedError

    expected_operations = {
        "workflow_run_operation_with_name_override": nexusrpc.Operation(
            name="operation-name",
            input_type=Input,
            output_type=Output,
        ),
    }


@pytest.mark.parametrize(
    "test_case",
    [
        NotCalled,
        CalledWithoutArgs,
        CalledWithNameOverride,
    ],
)
@pytest.mark.asyncio
async def test_collected_operation_names(
    test_case: type[_TestCase],
):
    service_defn = nexusrpc.get_service_definition(test_case.Service)
    assert isinstance(service_defn, nexusrpc.ServiceDefinition)
    assert service_defn.name == "Service"
    for method_name, expected_op in test_case.expected_operations.items():
        _, actual_op = get_operation_factory(getattr(test_case.Service, method_name))
        assert isinstance(actual_op, nexusrpc.Operation)
        assert actual_op.name == expected_op.name
        assert actual_op.input_type == expected_op.input_type
        assert actual_op.output_type == expected_op.output_type


def test_unsafe_narrow_parameter_annotations_warn_and_drop_input_type():
    """Narrower SDK parameter annotations prevent inference of the input type."""

    with pytest.warns(
        UserWarning,
        match="Expected parameter 1 .* TemporalNexusClient",
    ):

        class MyTemporalNexusClient(nexus.TemporalNexusClient):
            def custom_method(self):
                raise NotImplementedError

        class TemporalOperationHandler:
            @nexus.temporal_operation  # type: ignore[arg-type]
            async def op(
                self,
                _client: MyTemporalNexusClient,
                _input: Input,
            ) -> nexus.TemporalOperationResult[Output]:
                raise NotImplementedError

    _, temporal_op = get_operation_factory(TemporalOperationHandler.op)
    assert isinstance(temporal_op, nexusrpc.Operation)
    assert temporal_op.input_type is None
    assert temporal_op.output_type == Output

    with pytest.warns(
        UserWarning,
        match="Expected parameter 1 .* WorkflowRunOperationContext",
    ):

        class MyWorkflowRunOpCtx(nexus.WorkflowRunOperationContext):
            def custom_method(self):
                raise NotImplementedError

        class WorkflowRunOperationHandler:
            @workflow_run_operation  # type: ignore[arg-type]
            async def op(
                self,
                _ctx: MyWorkflowRunOpCtx,
                _input: Input,
            ) -> nexus.WorkflowHandle[Output]:
                raise NotImplementedError

    _, workflow_op = get_operation_factory(WorkflowRunOperationHandler.op)
    assert isinstance(workflow_op, nexusrpc.Operation)
    assert workflow_op.input_type is None
    assert workflow_op.output_type == Output


def test_safe_broader_parameter_annotations_preserve_input_type_without_warnings():
    """Broader SDK parameter annotations preserve inference of the input type."""

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")

        class TemporalOperationHandler:
            @nexus.temporal_operation
            async def op(
                self,
                _client: object,
                _input: Input,
            ) -> nexus.TemporalOperationResult[Output]:
                raise NotImplementedError

        class WorkflowRunStartContextHandler:
            @workflow_run_operation
            async def op(
                self,
                _ctx: nexusrpc.handler.StartOperationContext,
                _input: Input,
            ) -> nexus.WorkflowHandle[Output]:
                raise NotImplementedError

    assert not caught

    for method in (
        TemporalOperationHandler.op,
        WorkflowRunStartContextHandler.op,
    ):
        _, op = get_operation_factory(method)
        assert isinstance(op, nexusrpc.Operation)
        assert op.input_type == Input
        assert op.output_type == Output
