"""Create and delete an isolated Temporal Cloud namespace for CI."""

import asyncio
import os
import sys
import time
from pathlib import Path

from temporalio.api.cloud.cloudservice.v1 import (
    CreateNamespaceRequest,
    DeleteNamespaceRequest,
    GetAsyncOperationRequest,
    GetNamespaceRequest,
)
from temporalio.api.cloud.namespace.v1 import MtlsAuthSpec, NamespaceSpec
from temporalio.api.cloud.operation.v1 import AsyncOperation
from temporalio.client import CloudOperationsClient


async def wait_for_operation(
    client: CloudOperationsClient, operation: AsyncOperation
) -> None:
    deadline = time.monotonic() + 10 * 60
    while True:
        operation = (
            await client.cloud_service.get_async_operation(
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
                f"{operation.id} {AsyncOperation.State.Name(operation.state).lower()}: "
                f"{operation.failure_reason}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for Cloud operation {operation.id}")
        delay = max(
            operation.check_duration.seconds
            + operation.check_duration.nanos / 1_000_000_000,
            1,
        )
        await asyncio.sleep(min(delay, deadline - time.monotonic()))


async def create() -> None:
    client = await cloud_client()
    namespace_name = "sdk-python-ci-{}-{}".format(
        os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    )
    result = await client.cloud_service.create_namespace(
        CreateNamespaceRequest(
            spec=NamespaceSpec(
                name=namespace_name,
                regions=["aws-ca-central-1"],
                retention_days=1,
                mtls_auth=MtlsAuthSpec(
                    accepted_client_ca=Path(
                        os.environ["TEMPORAL_CLOUD_CLIENT_CA_PATH"]
                    ).read_bytes(),
                    enabled=True,
                ),
            )
        )
    )
    # Make cleanup possible even if provisioning fails after Cloud accepts the request.
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"namespace={result.namespace}\n")
    await wait_for_operation(client, result.async_operation)


async def delete(namespace: str) -> None:
    client = await cloud_client()
    existing = await client.cloud_service.get_namespace(
        GetNamespaceRequest(namespace=namespace)
    )
    result = await client.cloud_service.delete_namespace(
        DeleteNamespaceRequest(
            namespace=namespace,
            resource_version=existing.namespace.resource_version,
        )
    )
    await wait_for_operation(client, result.async_operation)


async def cloud_client() -> CloudOperationsClient:
    return await CloudOperationsClient.connect(
        api_key=os.environ["TEMPORAL_CLIENT_CLOUD_API_KEY"],
        version=os.environ["TEMPORAL_CLIENT_CLOUD_API_VERSION"],
    )


async def main() -> None:
    match sys.argv[1:]:
        case ["create"]:
            await create()
        case ["delete", namespace]:
            await delete(namespace)
        case _:
            raise ValueError("Usage: cloud_namespace.py create|delete <namespace>")


if __name__ == "__main__":
    asyncio.run(main())
