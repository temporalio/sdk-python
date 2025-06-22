from typing import Any, Optional, Type

import nexusrpc
import nexusrpc.handler
import pytest

import temporalio.api.failure.v1
import temporalio.nexus
from temporalio.nexus.handler._token import WorkflowOperationToken

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
        @nexusrpc.handler.sync_operation_handler
        async def op(
            self, ctx: nexusrpc.handler.StartOperationContext, input: None
        ) -> None: ...

    error_message = None


class ValidWorkflowRunImpl(_InterfaceImplementationTestCase):
    @nexusrpc.service
    class Interface:
        op: nexusrpc.Operation[str, int]

    class Impl:
        @temporalio.nexus.handler.workflow_run_operation_handler
        async def op(
            self, ctx: nexusrpc.handler.StartOperationContext, input: str
        ) -> WorkflowOperationToken[int]: ...

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
