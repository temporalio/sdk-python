import uuid

import nexusrpc
import nexusrpc.handler
import pytest

from temporalio import nexus, workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import make_nexus_endpoint_name


@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, input: int) -> int:
        return input + 1


class MyIncrementOperationHandler(nexusrpc.handler.OperationHandler[int, int]):
    async def start(
        self,
        ctx: nexusrpc.handler.StartOperationContext,
        input: int,
    ) -> nexusrpc.handler.StartOperationResultAsync:
        wrctx = nexus.WorkflowRunOperationContext._from_start_operation_context(ctx)
        wf_handle = await wrctx.start_workflow(
            MyWorkflow.run, input, id=str(uuid.uuid4())
        )
        return nexusrpc.handler.StartOperationResultAsync(token=wf_handle.to_token())

    async def cancel(
        self,
        ctx: nexusrpc.handler.CancelOperationContext,
        token: str,
    ) -> None:
        raise NotImplementedError


@workflow.defn
class IncrementCallerWorkflow:
    @workflow.run
    async def run(self, input: int, task_queue: str) -> int:
        client = workflow.create_nexus_client(
            service="MyService",
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        return await client.execute_operation("increment", input, output_type=int)


async def test_run_nexus_service_from_programmatically_created_service_handler(
    client: Client,
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())

    service_handler = nexusrpc.handler._core.ServiceHandler(
        service=nexusrpc.ServiceDefinition(
            name="MyService",
            operation_definitions={
                "increment": nexusrpc.OperationDefinition[int, int](
                    name="increment",
                    method_name="increment",
                    input_type=int,
                    output_type=int,
                ),
            },
        ),
        operation_handlers={
            "increment": MyIncrementOperationHandler(),
        },
    )

    await env.create_nexus_endpoint(make_nexus_endpoint_name(task_queue), task_queue)
    async with Worker(
        client,
        task_queue=task_queue,
        nexus_service_handlers=[service_handler],
        workflows=[IncrementCallerWorkflow, MyWorkflow],
    ):
        result = await client.execute_workflow(
            IncrementCallerWorkflow.run,
            args=[5, task_queue],
            id=str(uuid.uuid4()),
            task_queue=task_queue,
        )
        assert result == 6
