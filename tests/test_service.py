import inspect
import os
import re
from typing import Any, Callable, Dict, Mapping, Tuple, Type

import google.protobuf.empty_pb2
import google.protobuf.message
import grpc
import pytest

import temporalio
import temporalio.api.operatorservice.v1
import temporalio.api.testservice.v1
import temporalio.api.workflowservice.v1
import temporalio.service
from temporalio.client import Client


def test_all_grpc_calls_present(client: Client):
    def assert_all_calls_present(
        service: Any,
        package: Any,
        new_stub: Callable[[grpc.Channel], Any],
        custom_req_resp: Mapping[
            str,
            Tuple[
                Type[google.protobuf.message.Message],
                Type[google.protobuf.message.Message],
            ],
        ] = {},
    ) -> None:
        # Collect service calls
        service_calls: Dict[str, Tuple[Type, Type]] = {}
        for _, call in inspect.getmembers(service):
            if isinstance(call, temporalio.service.ServiceCall):
                service_calls[call.name] = (call.req_type, call.resp_type)

        # Collect gRPC service calls with a fake channel
        channel = CallCollectingChannel(package, custom_req_resp)
        new_stub(channel)

        # Confirm they are the same
        assert channel.calls == service_calls

    assert_all_calls_present(
        client.workflow_service,
        temporalio.api.workflowservice.v1,
        temporalio.api.workflowservice.v1.WorkflowServiceStub,
    )
    assert_all_calls_present(
        client.operator_service,
        temporalio.api.operatorservice.v1,
        temporalio.api.operatorservice.v1.OperatorServiceStub,
    )
    assert_all_calls_present(
        client.test_service,
        temporalio.api.testservice.v1,
        temporalio.api.testservice.v1.TestServiceStub,
        {
            # Abnormal req/resp
            "GetCurrentTime": (
                google.protobuf.empty_pb2.Empty,
                temporalio.api.testservice.v1.GetCurrentTimeResponse,
            ),
            "SleepUntil": (
                temporalio.api.testservice.v1.SleepUntilRequest,
                temporalio.api.testservice.v1.SleepResponse,
            ),
            "UnlockTimeSkippingWithSleep": (
                temporalio.api.testservice.v1.SleepRequest,
                temporalio.api.testservice.v1.SleepResponse,
            ),
        },
    )


class CallCollectingChannel(grpc.Channel):
    def __init__(
        self,
        package: Any,
        custom_req_resp: Mapping[
            str,
            Tuple[
                Type[google.protobuf.message.Message],
                Type[google.protobuf.message.Message],
            ],
        ],
    ) -> None:
        super().__init__()
        self.package = package
        self.custom_req_resp = custom_req_resp
        self.calls: Dict[str, Tuple[Type, Type]] = {}

    def unary_unary(self, method, request_serializer, response_deserializer):
        # Last part after slash
        name = method.rsplit("/", 1)[-1]
        req_resp = self.custom_req_resp.get(name, None) or (
            getattr(self.package, name + "Request"),
            getattr(self.package, name + "Response"),
        )
        # Camel to snake case
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        self.calls[name] = req_resp


CallCollectingChannel.__abstractmethods__ = set()


def test_version():
    # Extract version from pyproject.toml
    with open(
        os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), "r"
    ) as f:
        pyproject = f.read()
    version = pyproject[pyproject.find('version = "') + 11 :]
    version = version[: version.find('"')]
    assert temporalio.service.__version__ == version
    assert temporalio.__version__ == version


async def test_check_health(client: Client):
    assert await client.service_client.check_health()
    # Unknown service
    with pytest.raises(temporalio.service.RPCError) as err:
        assert await client.service_client.check_health(service="whatever")
    assert err.value.status == temporalio.service.RPCStatusCode.NOT_FOUND
