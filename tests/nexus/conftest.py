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
from temporalio.api.workflowservice.v1 import DeleteNexusOperationExecutionRequest
from temporalio.client import CloudOperationsClient, NexusOperationFailureError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment

logger = logging.getLogger(__name__)


@nexusrpc.service
class CloudNexusEndpointReadinessService:
    ready: nexusrpc.Operation[None, None]


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

    async def wait_for_endpoint_readiness(
        self, env: WorkflowEnvironment, endpoint_name: str
    ) -> None:
        deadline = time.monotonic() + 10 * 60
        nexus_client = env.client.create_nexus_client(
            CloudNexusEndpointReadinessService, endpoint_name
        )
        # Cloud reports endpoint activation before the Workflow Service can always
        # resolve it. Waiting for the disposable operation's terminal state verifies
        # routing, which happens asynchronously after its start request is accepted.
        attempts = 0
        while True:
            attempts += 1
            operation_id = f"cloud-nexus-readiness-{uuid.uuid4()}"
            try:
                operation = await nexus_client.start_operation(
                    CloudNexusEndpointReadinessService.ready,
                    None,
                    id=operation_id,
                    schedule_to_close_timeout=timedelta(seconds=5),
                )
            except RPCError as err:
                if (
                    err.status != RPCStatusCode.NOT_FOUND
                    or str(err) != "endpoint not found"
                ):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for Cloud Nexus endpoint {endpoint_name} "
                        "to become available"
                    ) from err
                if attempts == 1:
                    logger.info(
                        "Cloud Nexus endpoint %s is not yet resolvable; retrying "
                        "readiness operation %s",
                        endpoint_name,
                        operation_id,
                    )
                await asyncio.sleep(1)
                continue
            try:
                print(
                    "Waiting for Cloud Nexus endpoint "
                    f"{endpoint_name} readiness operation {operation_id}",
                    flush=True,
                )
                await operation.result()
            except NexusOperationFailureError as err:
                if str(err.cause) in {
                    "endpoint not registered",
                    "nexus endpoint not found",
                }:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for Cloud Nexus endpoint {endpoint_name} "
                            "to become available"
                        ) from err
                    if attempts == 1:
                        logger.info(
                            "Cloud Nexus endpoint %s is not yet routable; retrying "
                            "readiness operation %s",
                            endpoint_name,
                            operation_id,
                        )
                    await asyncio.sleep(1)
                    continue
            finally:
                logger.info(
                    "Deleting Cloud Nexus readiness operation %s for endpoint %s",
                    operation_id,
                    endpoint_name,
                )
                await env.client.workflow_service.delete_nexus_operation_execution(
                    DeleteNexusOperationExecutionRequest(
                        namespace=env.client.namespace,
                        operation_id=operation_id,
                    )
                )
            logger.info(
                "Cloud Nexus endpoint %s completed readiness operation %s after "
                "%d attempt(s)",
                endpoint_name,
                operation_id,
                attempts,
            )
            break


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
            "Cloud Nexus endpoint %s (%s) is active; checking Workflow Service readiness",
            endpoint_name,
            endpoint.id,
        )
        await cloud_nexus_endpoint_client.wait_for_endpoint_readiness(
            env, endpoint_name
        )
        logger.info("Cloud Nexus endpoint %s is ready for the test", endpoint_name)
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
