from typing import Any, Optional, Type

import nexusrpc
import nexusrpc.handler
import pytest
from nexusrpc.handler import StartOperationContext, sync_operation

from temporalio import nexus
from temporalio.nexus import WorkflowRunOperationContext, workflow_run_operation

HTTP_PORT = 7243


class _InterfaceImplementationTestCase:
    Interface: Type[Any]
    Impl: Type[Any]
    error_message: Optional[str]


class ValidImpl(_InterfaceImplementationTestCase):
    @nexusrpc.service
    class Interface:
        op: nexusrpc.Operation[None, None]

    class Impl:
        @sync_operation
        async def op(self, ctx: StartOperationContext, input: None) -> None: ...

    error_message = None


class ValidWorkflowRunImpl(_InterfaceImplementationTestCase):
    @nexusrpc.service
    class Interface:
        op: nexusrpc.Operation[str, int]

    class Impl:
        @workflow_run_operation
        async def op(
            self, ctx: WorkflowRunOperationContext, input: str
        ) -> nexus.WorkflowHandle[int]:
            raise NotImplementedError

    error_message = None


@pytest.mark.parametrize(
    "test_case",
    [
        ValidImpl,
        ValidWorkflowRunImpl,
    ],
)
def test_service_decorator_enforces_interface_conformance(
    test_case: Type[_InterfaceImplementationTestCase],
):
    if test_case.error_message:
        with pytest.raises(Exception) as ei:
            nexusrpc.handler.service_handler(test_case.Interface)(test_case.Impl)
        err = ei.value
        assert test_case.error_message in str(err)
    else:
        nexusrpc.handler.service_handler(service=test_case.Interface)(test_case.Impl)
