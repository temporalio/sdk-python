"""
Test that workflow_run_operation_handler decorator results in operation definitions with the correct name
and input/output types.
"""

from dataclasses import dataclass
from typing import Any, Type

import nexusrpc.handler
import pytest

import temporalio.nexus.handler
from temporalio.client import WorkflowHandle


@dataclass
class Input:
    pass


@dataclass
class Output:
    pass


@dataclass
class _TestCase:
    Service: Type[Any]
    expected_operations: dict[str, nexusrpc.contract.Operation]


class NotCalled(_TestCase):
    @nexusrpc.handler.service_handler
    class Service:
        @temporalio.nexus.handler.workflow_run_operation_handler
        async def workflow_run_operation_handler(
            self, ctx: nexusrpc.handler.StartOperationContext, input: Input
        ) -> WorkflowHandle[Any, Output]: ...

    expected_operations = {
        "workflow_run_operation_handler": nexusrpc.contract.Operation._create(
            name="workflow_run_operation_handler",
            input_type=Input,
            output_type=Output,
        ),
    }


class CalledWithoutArgs(_TestCase):
    @nexusrpc.handler.service_handler
    class Service:
        @temporalio.nexus.handler.workflow_run_operation_handler()
        async def workflow_run_operation_handler(
            self, ctx: nexusrpc.handler.StartOperationContext, input: Input
        ) -> WorkflowHandle[Any, Output]: ...

    expected_operations = NotCalled.expected_operations


class CalledWithNameOverride(_TestCase):
    @nexusrpc.handler.service_handler
    class Service:
        @temporalio.nexus.handler.workflow_run_operation_handler(name="operation-name")
        async def workflow_run_operation_with_name_override(
            self, ctx: nexusrpc.handler.StartOperationContext, input: Input
        ) -> WorkflowHandle[Any, Output]: ...

    expected_operations = {
        "workflow_run_operation_with_name_override": nexusrpc.contract.Operation._create(
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
    test_case: Type[_TestCase],
):
    service: nexusrpc.contract.Service = getattr(test_case.Service, "__nexus_service__")
    assert isinstance(service, nexusrpc.contract.Service)
    assert service.name == "Service"
    for method_name, expected_op in test_case.expected_operations.items():
        actual_op = getattr(test_case.Service, method_name).__nexus_operation__
        assert isinstance(actual_op, nexusrpc.contract.Operation)
        assert actual_op.name == expected_op.name
        assert actual_op.input_type == expected_op.input_type
        assert actual_op.output_type == expected_op.output_type
