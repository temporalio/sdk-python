"""Tests for Nexus worker client updates."""

import uuid

import nexusrpc
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation

import temporalio.nexus
from temporalio import workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


@nexusrpc.service
class ClientTestService:
    capture_client: nexusrpc.Operation[None, str]


captured_clients: list[Client] = []


@service_handler(service=ClientTestService)
class ClientTestServiceHandler:
    @sync_operation
    async def capture_client(self, _ctx: StartOperationContext, _input: None) -> str:
        captured_clients.append(temporalio.nexus.client())
        return "done"


@workflow.defn
class ClientTestCallerWorkflow:
    @workflow.run
    async def run(self, endpoint_name: str) -> str:
        nexus_client = workflow.create_nexus_client(
            service=ClientTestService,
            endpoint=endpoint_name,
        )
        return await nexus_client.execute_operation(
            ClientTestService.capture_client,
            None,
        )


async def test_nexus_client_updates_when_worker_client_changes(
    env: WorkflowEnvironment,
):
    """Test that Nexus operations get the updated client when worker.client is changed."""
    # Create a second client (simulating a new client after cert rotation)
    # Must use the same runtime
    client2 = await Client.connect(
        env.client.service_client.config.target_host,
        namespace=env.client.namespace,
        data_converter=env.client.data_converter,
        runtime=env.client.service_client.config.runtime,
    )

    # Clear any previous captures
    captured_clients.clear()

    caller_task_queue = f"caller-{uuid.uuid4()}"
    handler_task_queue = f"handler-{uuid.uuid4()}"

    # Create Nexus endpoint
    endpoint_name = "test-endpoint"
    await env.create_nexus_endpoint(endpoint_name, handler_task_queue)

    # Caller worker
    caller_worker = Worker(
        env.client,
        task_queue=caller_task_queue,
        workflows=[ClientTestCallerWorkflow],
    )

    # Handler worker
    handler_worker = Worker(
        env.client,
        task_queue=handler_task_queue,
        nexus_service_handlers=[ClientTestServiceHandler()],
    )

    async with caller_worker, handler_worker:
        # Execute operation with original client
        await env.client.execute_workflow(
            ClientTestCallerWorkflow.run,
            endpoint_name,
            id=f"wf-{uuid.uuid4()}",
            task_queue=caller_task_queue,
        )

        # Update handler worker's client
        handler_worker.client = client2

        # Execute operation again - should get the new client
        await client2.execute_workflow(
            ClientTestCallerWorkflow.run,
            endpoint_name,
            id=f"wf-{uuid.uuid4()}",
            task_queue=caller_task_queue,
        )

    # Should have captured both clients
    assert len(captured_clients) == 2
    assert captured_clients[0] is env.client
    assert captured_clients[1] is client2  # This will fail before the fix
