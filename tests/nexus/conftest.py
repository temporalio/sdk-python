import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import timedelta

import nexusrpc
import pytest
import pytest_asyncio
from nexusrpc.handler import StartOperationContext, service_handler, sync_operation

from temporalio.api.cloud.cloudservice.v1 import (
    CreateNexusEndpointRequest,
    DeleteNexusEndpointRequest,
    GetAsyncOperationRequest,
    GetNamespaceRequest,
    GetNexusEndpointRequest,
)
from temporalio.api.cloud.nexus.v1 import (
    AllowedCloudNamespacePolicySpec,
    Endpoint,
    EndpointPolicySpec,
    EndpointSpec,
    EndpointTargetSpec,
    WorkerTargetSpec,
)
from temporalio.api.cloud.operation.v1 import AsyncOperation
from temporalio.api.cloud.resource.v1 import ResourceState
from temporalio.client import CloudOperationsClient, NexusOperationFailureError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.helpers.nexus import make_nexus_endpoint_name

logger = logging.getLogger(__name__)


@dataclass
class _CloudNexusEndpointClient:
    client: CloudOperationsClient
    namespace_id: str

    async def wait_for_operation(self, operation: AsyncOperation) -> None:
        deadline = time.monotonic() + 10 * 60
        while True:
            operation = (
                await self.client.cloud_service.get_async_operation(
                    GetAsyncOperationRequest(async_operation_id=operation.id)
                )
            ).async_operation
            if operation.state == AsyncOperation.STATE_FULFILLED:
                return
            if operation.state in {
                AsyncOperation.STATE_FAILED,
                AsyncOperation.STATE_CANCELLED,
                AsyncOperation.STATE_REJECTED,
            }:
                raise RuntimeError(
                    "Cloud operation "
                    f"{operation.id} "
                    f"{AsyncOperation.State.Name(operation.state).lower()}: "
                    f"{operation.failure_reason}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Cloud operation {operation.id}"
                )
            delay = max(
                operation.check_duration.seconds
                + operation.check_duration.nanos / 1_000_000_000,
                1,
            )
            await asyncio.sleep(min(delay, deadline - time.monotonic()))

    async def wait_for_endpoint(self, endpoint_id: str) -> Endpoint:
        deadline = time.monotonic() + 10 * 60
        while True:
            endpoint = (
                await self.client.cloud_service.get_nexus_endpoint(
                    GetNexusEndpointRequest(endpoint_id=endpoint_id)
                )
            ).endpoint
            if endpoint.state == ResourceState.RESOURCE_STATE_ACTIVE:
                return endpoint
            if endpoint.state in {
                ResourceState.RESOURCE_STATE_ACTIVATION_FAILED,
                ResourceState.RESOURCE_STATE_UPDATE_FAILED,
                ResourceState.RESOURCE_STATE_DELETE_FAILED,
                ResourceState.RESOURCE_STATE_SUSPENDED,
                ResourceState.RESOURCE_STATE_EXPIRED,
            }:
                raise RuntimeError(
                    "Cloud Nexus endpoint "
                    f"{endpoint_id} "
                    f"{ResourceState.Name(endpoint.state).lower()}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Cloud Nexus endpoint {endpoint_id}"
                )
            await asyncio.sleep(1)


@dataclass(frozen=True)
class NexusEndpoint:
    name: str
    task_queue: str


@nexusrpc.service
class _EndpointReadinessService:
    ready: nexusrpc.Operation[None, None]


@service_handler(service=_EndpointReadinessService)
class _EndpointReadinessHandler:
    @sync_operation
    async def ready(self, _ctx: StartOperationContext, _input: None) -> None:
        return None


@pytest_asyncio.fixture(scope="session")  # type: ignore[reportUntypedFunctionDecorator]
async def cloud_nexus_endpoint_client() -> AsyncGenerator[
    _CloudNexusEndpointClient | None, None
]:
    if "TEMPORAL_IS_CLOUD_TESTS" not in os.environ:
        yield None
        return

    client = await CloudOperationsClient.connect(
        api_key=os.environ["TEMPORAL_CLIENT_CLOUD_API_KEY"],
        version=os.environ["TEMPORAL_CLIENT_CLOUD_API_VERSION"],
    )
    namespace = await client.cloud_service.get_namespace(
        GetNamespaceRequest(namespace=os.environ["TEMPORAL_NAMESPACE"])
    )
    yield _CloudNexusEndpointClient(client, namespace.namespace.namespace)


@pytest_asyncio.fixture(autouse=True)  # type: ignore[reportUntypedFunctionDecorator]
async def cloud_nexus_endpoints(
    cloud_nexus_endpoint_client: _CloudNexusEndpointClient | None,
    env: WorkflowEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[None, None]:
    if cloud_nexus_endpoint_client is None:
        yield
        return

    endpoints: list[Endpoint] = []

    async def create_nexus_endpoint(endpoint_name: str, task_queue: str) -> Endpoint:
        logger.info(
            "Creating Cloud Nexus endpoint %s for task queue %s",
            endpoint_name,
            task_queue,
        )
        response = await cloud_nexus_endpoint_client.client.cloud_service.create_nexus_endpoint(
            CreateNexusEndpointRequest(
                spec=EndpointSpec(
                    name=endpoint_name,
                    target_spec=EndpointTargetSpec(
                        worker_target_spec=WorkerTargetSpec(
                            namespace_id=cloud_nexus_endpoint_client.namespace_id,
                            task_queue=task_queue,
                        )
                    ),
                    policy_specs=[
                        EndpointPolicySpec(
                            allowed_cloud_namespace_policy_spec=AllowedCloudNamespacePolicySpec(
                                namespace_id=cloud_nexus_endpoint_client.namespace_id
                            )
                        )
                    ],
                )
            )
        )
        await cloud_nexus_endpoint_client.wait_for_operation(response.async_operation)
        endpoint = (
            await cloud_nexus_endpoint_client.client.cloud_service.get_nexus_endpoint(
                GetNexusEndpointRequest(endpoint_id=response.endpoint_id)
            )
        ).endpoint
        endpoints.append(endpoint)
        endpoint = await cloud_nexus_endpoint_client.wait_for_endpoint(
            response.endpoint_id
        )
        endpoints[-1] = endpoint
        logger.info(
            "Cloud Nexus endpoint %s (%s) is active", endpoint_name, endpoint.id
        )
        return endpoint

    monkeypatch.setattr(env, "create_nexus_endpoint", create_nexus_endpoint)
    try:
        yield
    finally:
        for endpoint in reversed(endpoints):
            logger.info(
                "Deleting Cloud Nexus endpoint %s (%s)", endpoint.spec.name, endpoint.id
            )
            response = await cloud_nexus_endpoint_client.client.cloud_service.delete_nexus_endpoint(
                DeleteNexusEndpointRequest(
                    endpoint_id=endpoint.id,
                    resource_version=endpoint.resource_version,
                )
            )
            await cloud_nexus_endpoint_client.wait_for_operation(
                response.async_operation
            )


@pytest_asyncio.fixture
async def nexus_endpoint(
    cloud_nexus_endpoint_client: _CloudNexusEndpointClient | None,
    env: WorkflowEnvironment,
) -> NexusEndpoint:
    """Create and, on Cloud, route-check a Nexus endpoint before a test worker."""
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with time-skipping server")

    task_queue = str(uuid.uuid4())
    endpoint = NexusEndpoint(
        name=make_nexus_endpoint_name(task_queue), task_queue=task_queue
    )
    await env.create_nexus_endpoint(endpoint.name, endpoint.task_queue)

    if cloud_nexus_endpoint_client is None:
        return endpoint

    deadline = time.monotonic() + 10 * 60
    nexus_client = env.client.create_nexus_client(
        _EndpointReadinessService, endpoint.name
    )
    attempt = 0
    async with Worker(
        env.client,
        task_queue=endpoint.task_queue,
        nexus_service_handlers=[_EndpointReadinessHandler()],
    ):
        while True:
            attempt += 1
            try:
                operation = await nexus_client.start_operation(
                    _EndpointReadinessService.ready,
                    None,
                    id=f"cloud-nexus-readiness-{uuid.uuid4()}",
                    schedule_to_close_timeout=timedelta(seconds=10),
                )
                await asyncio.wait_for(operation.result(), timeout=15)
                break
            except RPCError as err:
                retryable = (
                    err.status == RPCStatusCode.NOT_FOUND
                    and str(err) == "endpoint not found"
                )
            except NexusOperationFailureError as err:
                retryable = str(err.cause) in {
                    "endpoint not registered",
                    "nexus endpoint not found",
                }
            except TimeoutError:
                retryable = True
            if not retryable:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Cloud Nexus endpoint {endpoint.name} "
                    "to route operations"
                )
            logger.info(
                "Cloud Nexus endpoint %s did not route readiness operation on "
                "attempt %d; retrying",
                endpoint.name,
                attempt,
            )
            await asyncio.sleep(1)
    return endpoint
