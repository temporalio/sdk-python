import inspect
import os
import re
from datetime import timedelta
from typing import Any, Callable, Dict, Mapping, Tuple, Type

import google.protobuf.empty_pb2
import google.protobuf.message
import google.protobuf.symbol_database
import grpc
import pytest
from google.protobuf.descriptor import FileDescriptor, MethodDescriptor

import temporalio
import temporalio.api.cloud.cloudservice.v1
import temporalio.api.cloud.cloudservice.v1.service_pb2
import temporalio.api.errordetails.v1
import temporalio.api.operatorservice.v1
import temporalio.api.operatorservice.v1.service_pb2
import temporalio.api.testservice.v1
import temporalio.api.testservice.v1.service_pb2
import temporalio.api.workflowservice.v1
import temporalio.api.workflowservice.v1.service_pb2
import temporalio.bridge.proto.health.v1.health_pb2
import temporalio.service
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


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
        service_calls = set()
        for name, call in inspect.getmembers(service):
            # ignore private methods and non-rpc members "client" and "service"
            if name[0] != "_" and name != "client" and name != "service":
                service_calls.add(name)

        # Collect gRPC service calls with a fake channel
        channel = CallCollectingChannel(package, custom_req_resp)
        new_stub(channel)

        # Confirm they are the same
        missing = channel.calls.keys() - service_calls
        assert not missing
        added = service_calls - channel.calls.keys()
        assert not added

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
        client.service_client.cloud_service,
        temporalio.api.cloud.cloudservice.v1,
        temporalio.api.cloud.cloudservice.v1.CloudServiceStub,
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
        name = _camel_to_snake(name)
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


async def test_grpc_status(client: Client, env: WorkflowEnvironment):
    if env.supports_time_skipping:
        pytest.skip(
            "Java test server: https://github.com/temporalio/sdk-java/issues/1557"
        )
    # Try to make a simple client call on a non-existent namespace
    with pytest.raises(temporalio.service.RPCError) as err:
        await client.workflow_service.describe_namespace(
            temporalio.api.workflowservice.v1.DescribeNamespaceRequest(
                namespace="does not exist",
            )
        )
    # Confirm right failure type
    assert not err.value.grpc_status.details[0].Is(
        temporalio.api.errordetails.v1.QueryFailedFailure.DESCRIPTOR
    )
    assert err.value.grpc_status.details[0].Is(
        temporalio.api.errordetails.v1.NamespaceNotFoundFailure.DESCRIPTOR
    )


async def test_rpc_execution_not_unknown(client: Client):
    """
    Execute each rpc method and expect a failure, but ensure the failure is not that the rpc method is unknown
    """
    sym_db = google.protobuf.symbol_database.Default()
    service_client = client.service_client

    async def test_method(
        target_service_name: str, method_descriptor: MethodDescriptor
    ):
        if method_descriptor.client_streaming or method_descriptor.server_streaming:
            # skip streaming calls
            return

        method_name = _camel_to_snake(method_descriptor.name)

        # get request type and instantiate an empty request
        request_type = sym_db.GetSymbol(method_descriptor.input_type.full_name)
        request = request_type()

        # get the appropriate temporal service from the service_client
        target_service = getattr(service_client, target_service_name)

        # execute rpc and ensure that any exception that occurs is not the
        # "Unknown RPC call" error which indicates the python and rust rpc components
        # should be regenerated
        rpc_call = getattr(target_service, method_name)
        try:
            await rpc_call(request, timeout=timedelta(milliseconds=1))
        except ValueError as err:
            assert (
                "Unknown RPC call" not in str(err)
            ), f"Unexpected unknown-RPC error for {target_service_name}.{method_name}: {err}"
        except temporalio.service.RPCError:
            pass

    async def test_service(
        *, proto_module: FileDescriptor, proto_service: str, target_service_name: str
    ):
        # load the module and test each method of the specified service
        service_descriptor = proto_module.services_by_name[proto_service]

        for method_descriptor in service_descriptor.methods:
            await test_method(target_service_name, method_descriptor)

    await test_service(
        proto_module=temporalio.api.workflowservice.v1.service_pb2.DESCRIPTOR,
        proto_service="WorkflowService",
        target_service_name="workflow_service",
    )
    await test_service(
        proto_module=temporalio.api.operatorservice.v1.service_pb2.DESCRIPTOR,
        proto_service="OperatorService",
        target_service_name="operator_service",
    )
    await test_service(
        proto_module=temporalio.api.cloud.cloudservice.v1.service_pb2.DESCRIPTOR,
        proto_service="CloudService",
        target_service_name="cloud_service",
    )
    await test_service(
        proto_module=temporalio.api.testservice.v1.service_pb2.DESCRIPTOR,
        proto_service="TestService",
        target_service_name="test_service",
    )
    await test_service(
        proto_module=temporalio.bridge.proto.health.v1.health_pb2.DESCRIPTOR,
        proto_service="Health",
        target_service_name="health_service",
    )
