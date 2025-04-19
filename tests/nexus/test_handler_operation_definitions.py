"""
Test that workflow_run_operation decorator results in operation definitions with the correct name
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
    expected_operations: dict[str, nexusrpc.handler.NexusOperationDefinition]


class NotCalled(_TestCase):
    @nexusrpc.handler.service
    class Service:
        @temporalio.nexus.handler.workflow_run_operation
        async def workflow_run_operation(
            self, ctx: nexusrpc.handler.StartOperationContext, input: Input
        ) -> WorkflowHandle[Any, Output]: ...

    expected_operations = {
        "workflow_run_operation": nexusrpc.handler.NexusOperationDefinition(
            name="workflow_run_operation",
            input_type=Input,
            output_type=Output,
        ),
    }


class CalledWithoutArgs(_TestCase):
    @nexusrpc.handler.service
    class Service:
        @temporalio.nexus.handler.workflow_run_operation()
        async def workflow_run_operation(
            self, ctx: nexusrpc.handler.StartOperationContext, input: Input
        ) -> WorkflowHandle[Any, Output]: ...

    expected_operations = NotCalled.expected_operations


class CalledWithNameOverride(_TestCase):
    @nexusrpc.handler.service
    class Service:
        @temporalio.nexus.handler.workflow_run_operation(name="operation-name")
        async def workflow_run_operation_with_name_override(
            self, ctx: nexusrpc.handler.StartOperationContext, input: Input
        ) -> WorkflowHandle[Any, Output]: ...

    expected_operations = {
        "workflow_run_operation_with_name_override": nexusrpc.handler.NexusOperationDefinition(
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
    defn: nexusrpc.handler.NexusServiceDefinition = getattr(
        test_case.Service, "__nexus_service__"
    )
    assert isinstance(defn, nexusrpc.handler.NexusServiceDefinition)
    assert defn.name == "Service"
    for method_name, expected_defn in test_case.expected_operations.items():
        actual_defn = getattr(test_case.Service, method_name).__nexus_operation__
        assert isinstance(actual_defn, nexusrpc.handler.NexusOperationDefinition)
        assert actual_defn.name == expected_defn.name
        assert actual_defn.input_type == expected_defn.input_type
        assert actual_defn.output_type == expected_defn.output_type
