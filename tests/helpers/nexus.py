import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import temporalio.api.failure.v1
import temporalio.api.nexus.v1
import temporalio.api.operatorservice.v1
import temporalio.workflow
from temporalio.client import Client
from temporalio.converter import FailureConverter, PayloadConverter
from temporalio.testing import WorkflowEnvironment

with temporalio.workflow.unsafe.imports_passed_through():
    import httpx
    from google.protobuf import json_format


def make_nexus_endpoint_name(task_queue: str) -> str:
    # Create endpoints for different task queues without name collisions.
    return f"nexus-endpoint-{task_queue}"


# TODO(nexus-preview): How do we recommend that users create endpoints in their own tests?
# See https://github.com/temporalio/sdk-typescript/pull/1708/files?show-viewed-files=true&file-filters%5B%5D=&w=0#r2082549085
async def create_nexus_endpoint(
    task_queue: str, client: Client
) -> temporalio.api.operatorservice.v1.CreateNexusEndpointResponse:
    name = make_nexus_endpoint_name(task_queue)
    return await client.operator_service.create_nexus_endpoint(
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


@dataclass
class ServiceClient:
    server_address: str  # E.g. http://127.0.0.1:7243
    endpoint: str
    service: str

    async def start_operation(
        self,
        operation: str,
        body: dict[str, Any] | None = None,
        headers: Mapping[str, str] = {},
    ) -> httpx.Response:
        """
        Start a Nexus operation.
        """
        # TODO(nexus-preview): Support callback URL as query param
        async with httpx.AsyncClient() as http_client:
            return await http_client.post(
                f"http://{self.server_address}/nexus/endpoints/{self.endpoint}/services/{self.service}/{operation}",
                json=body,
                headers=headers,
            )

    async def cancel_operation(
        self,
        operation: str,
        token: str,
    ) -> httpx.Response:
        async with httpx.AsyncClient() as http_client:
            return await http_client.post(
                f"http://{self.server_address}/nexus/endpoints/{self.endpoint}/services/{self.service}/{operation}/cancel",
                # Token can also be sent as "Nexus-Operation-Token" header
                params={"token": token},
            )

    @staticmethod
    def default_server_address(env: WorkflowEnvironment) -> str:
        # TODO(nexus-preview): nexus tests are making http requests directly but this is
        # not officially supported.
        parsed = urlparse(env.client.service_client.config.target_host)
        host = parsed.hostname or "127.0.0.1"
        http_port = getattr(env, "_http_port", 7243)
        return f"{host}:{http_port}"


def dataclass_as_dict(dataclass: Any) -> dict[str, Any]:
    """
    Return a shallow dict of the dataclass's fields.

    dataclasses.as_dict goes too far (attempts to pickle values)
    """
    return {
        field.name: getattr(dataclass, field.name)
        for field in dataclasses.fields(dataclass)
    }


@dataclass
class Failure:
    """A Nexus Failure object, with details parsed into an exception.

    https://github.com/nexus-rpc/api/blob/main/SPEC.md#failure
    """

    message: str = ""
    metadata: dict[str, str] | None = None
    details: dict[str, Any] | None = None

    exception_from_details: BaseException | None = dataclasses.field(
        init=False, default=None
    )

    def __post_init__(self) -> None:
        if self.metadata and (error_type := self.metadata.get("type")):
            self.exception_from_details = self._instantiate_exception(
                error_type, self.details
            )

    def _instantiate_exception(
        self, error_type: str, details: dict[str, Any] | None
    ) -> BaseException:
        proto = {
            "temporal.api.failure.v1.Failure": temporalio.api.failure.v1.Failure,
        }[error_type]()
        json_format.ParseDict(self.details, proto, ignore_unknown_fields=True)
        return FailureConverter.default.from_failure(proto, PayloadConverter.default)
