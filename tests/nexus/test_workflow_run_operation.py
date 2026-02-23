import re
import uuid
from dataclasses import dataclass
from typing import Any

import nexusrpc
import pytest
from nexusrpc import Operation, service
from nexusrpc.handler import (
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    service_handler,
)
from nexusrpc.handler._decorators import operation_handler

from temporalio import workflow
from temporalio.nexus import WorkflowRunOperationContext
from temporalio.nexus._operation_handlers import WorkflowRunOperationHandler
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import (
    Failure,
    ServiceClient,
    dataclass_as_dict,
    make_nexus_endpoint_name,
)


@dataclass
class Input:
    value: str


@workflow.defn
class EchoWorkflow:
    @workflow.run
    async def run(self, input: str) -> str:
        return input


class MyOperation(WorkflowRunOperationHandler):
    # TODO(nexus-preview) WorkflowRunOperationHandler is not currently implemented to
    # support subclassing as this test does.
    def __init__(self):  # type: ignore[reportMissingSuperCall]
        pass

    async def start(
        self, ctx: StartOperationContext, input: Input
    ) -> StartOperationResultAsync:
        tctx = WorkflowRunOperationContext._from_start_operation_context(ctx)
        handle = await tctx.start_workflow(
            EchoWorkflow.run,
            input.value,
            id=str(uuid.uuid4()),
        )
        return StartOperationResultAsync(handle.to_token())


@service_handler
class SubclassingHappyPath:
    @operation_handler
    def op(self) -> OperationHandler[Input, str]:
        return MyOperation()


@service
class Service:
    op: Operation[Input, str]


@service_handler(service=Service)
class SubclassingNoInputOutputTypeAnnotationsWithServiceDefinition:
    # Despite the lack of annotations on the service impl, the service definition
    # provides the type needed to deserialize the input into Input so that input.value
    # succeeds.
    @operation_handler
    def op(self) -> OperationHandler:
        return MyOperation()


@pytest.mark.parametrize(
    "service_handler_cls",
    [
        SubclassingHappyPath,
        SubclassingNoInputOutputTypeAnnotationsWithServiceDefinition,
    ],
)
async def test_workflow_run_operation(
    env: WorkflowEnvironment,
    service_handler_cls: type[Any],
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    endpoint = (
        await env.create_nexus_endpoint(
            make_nexus_endpoint_name(task_queue), task_queue
        )
    ).id
    assert (service_defn := nexusrpc.get_service_definition(service_handler_cls))
    service_client = ServiceClient(
        server_address=ServiceClient.default_server_address(env),
        endpoint=endpoint,
        service=service_defn.name,
    )
    async with Worker(
        env.client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler_cls()],
    ):
        resp = await service_client.start_operation(
            "op",
            dataclass_as_dict(Input(value="test")),
        )
        if hasattr(service_handler_cls, "__expected__error__"):
            status_code, message = service_handler_cls.__expected__error__
            assert resp.status_code == status_code
            failure = Failure(**resp.json())
            assert re.search(message, failure.message)
        else:
            assert resp.status_code == 201
