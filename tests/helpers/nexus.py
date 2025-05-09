import temporalio.api
import temporalio.api.common
import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.nexus
import temporalio.api.nexus.v1
import temporalio.api.operatorservice
import temporalio.api.operatorservice.v1
import temporalio.nexus
import temporalio.nexus.handler
from temporalio.client import Client


def make_nexus_endpoint_name(task_queue: str) -> str:
    # Create endpoints for different task queues without name collisions.
    return f"nexus-endpoint-{task_queue}"


async def create_nexus_endpoint(task_queue: str, client: Client) -> str:
    name = make_nexus_endpoint_name(task_queue)
    await client.operator_service.create_nexus_endpoint(
        temporalio.api.operatorservice.v1.CreateNexusEndpointRequest(
            spec=temporalio.api.nexus.v1.EndpointSpec(
                name=name,
                target=temporalio.api.nexus.v1.EndpointTarget(
                    worker=temporalio.api.nexus.v1.EndpointTarget.Worker(
                        namespace=client.namespace,
                        task_queue=task_queue,
                    )
                ),
            )
        )
    )
    return name
