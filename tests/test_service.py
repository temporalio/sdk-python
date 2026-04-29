import inspect
import os
import re
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

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
            tuple[
                type[google.protobuf.message.Message],
                type[google.protobuf.message.Message],
            ],
        ] = {},
    ) -> None:
        # Collect service calls
        service_calls = set()
        for name, _call in inspect.getmembers(service):
            # ignore private methods and non-rpc members "client" and "service"
            if name[0] != "_" and name != "client" and name != "service":
                service_calls.add(name)

        # Collect gRPC service calls with a fake channel
        channel = CallCollectingChannel(package, custom_req_resp)  # type: ignore
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
            tuple[
                type[google.protobuf.message.Message],
                type[google.protobuf.message.Message],
            ],
        ],
    ) -> None:
        super().__init__()
        self.package = package
        self.custom_req_resp = custom_req_resp
        self.calls: dict[str, tuple[type, type]] = {}

    def unary_unary(self, method, request_serializer, response_deserializer):  # type: ignore[reportIncompatibleMethodOverride]
        # Last part after slash
        name = method.rsplit("/", 1)[-1]
        req_resp = self.custom_req_resp.get(name, None) or (
            getattr(self.package, name + "Request"),
            getattr(self.package, name + "Response"),
        )
        # Camel to snake case
        name = _camel_to_snake(name)
        self.calls[name] = req_resp


CallCollectingChannel.__abstractmethods__ = set()  # type: ignore[reportAttributeAccessIssue]


def test_version():
    # Extract version from pyproject.toml
    with open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml")) as f:
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


def test_connect_config_tls_enabled_by_default_when_api_key_provided():
    """Test that TLS is enabled by default when API key is provided and tls is not configured."""
    config = temporalio.service.ConnectConfig(
        target_host="localhost:7233",
        api_key="test-api-key",
    )
    # TLS should be auto-enabled when api_key is provided and tls not explicitly set
    bridge_config = config._to_bridge_config()
    assert bridge_config.target_url == "https://localhost:7233"
    assert bridge_config.tls_config is not None


def test_connect_config_tls_can_be_explicitly_disabled_even_when_api_key_provided():
    """Test that TLS can be explicitly disabled even when API key is provided."""
    config = temporalio.service.ConnectConfig(
        target_host="localhost:7233",
        api_key="test-api-key",
        tls=False,
    )
    # TLS should remain disabled when explicitly set to False
    assert config.tls is False


def test_connect_config_tls_disabled_by_default_when_no_api_key():
    """Test that TLS is disabled by default when no API key is provided."""
    config = temporalio.service.ConnectConfig(
        target_host="localhost:7233",
    )
    # TLS should remain disabled when no api_key is provided
    bridge_config = config._to_bridge_config()
    assert bridge_config.target_url == "http://localhost:7233"
    assert bridge_config.tls_config is None


def test_connect_config_tls_explicit_config_preserved():
    """Test that explicit TLS configuration is preserved regardless of API key."""
    tls_config = temporalio.service.TLSConfig(
        server_root_ca_cert=b"test-cert",
        domain="test-domain",
    )
    config = temporalio.service.ConnectConfig(
        target_host="localhost:7233",
        api_key="test-api-key",
        tls=tls_config,
    )
    # Explicit TLS config should be preserved
    assert config.tls == tls_config


def test_connect_config_override_origin_forwarded_to_bridge():
    """override_origin is passed through to the bridge ClientConfig."""
    config = temporalio.service.ConnectConfig(
        target_host="localhost:7233",
        override_origin="http://temporal-frontend",
    )
    bridge_config = config._to_bridge_config()
    assert bridge_config.override_origin == "http://temporal-frontend"


def test_connect_config_override_origin_defaults_to_none():
    """override_origin defaults to None and is forwarded as None."""
    config = temporalio.service.ConnectConfig(target_host="localhost:7233")
    bridge_config = config._to_bridge_config()
    assert bridge_config.override_origin is None


async def test_override_origin_end_to_end():
    """override_origin reaches the gRPC channel and the client can connect.

    A minimal grpc.aio server is spun up on localhost. We connect the
    Temporal service client with override_origin set to an arbitrary value.
    The connection succeeds because the fake server is reachable; the
    override_origin value changes the HTTP/2 :authority pseudo-header on every
    call (verified by the Rust bridge unit tests). This test confirms the full
    Python stack — from ConnectConfig down through the Rust bridge — does not
    break when override_origin is set.

    Note: Python's grpc.aio server does not expose the :authority pseudo-header
    in ServicerContext.invocation_metadata() (it is an HTTP/2 transport-level
    header, not an application-level gRPC metadata entry). The per-header
    behaviour is covered by the Rust Core integration tests for
    ConnectionOptions::override_origin.
    """
    import socket as _socket

    import grpc.aio
    from temporalio.api.workflowservice.v1 import (
        request_response_pb2 as ws_pb2,
        service_pb2_grpc as ws_grpc,
    )

    received_calls: list[dict] = []

    class _FakeWorkflowService(ws_grpc.WorkflowServiceServicer):  # type: ignore[misc]
        async def GetSystemInfo(self, request, context):  # type: ignore[override]
            received_calls.append({"metadata": dict(context.invocation_metadata())})
            return ws_pb2.GetSystemInfoResponse(server_version="test")

    # Pick a free port.
    with _socket.socket() as _s:
        _s.bind(("127.0.0.1", 0))
        port = _s.getsockname()[1]

    server = grpc.aio.server()
    ws_grpc.add_WorkflowServiceServicer_to_server(_FakeWorkflowService(), server)  # type: ignore[arg-type]
    server.add_insecure_port(f"127.0.0.1:{port}")
    await server.start()
    try:
        config = temporalio.service.ConnectConfig(
            target_host=f"localhost:{port}",
            override_origin="http://temporal-frontend",
        )
        await temporalio.service.ServiceClient.connect(config)
    finally:
        await server.stop(0)

    assert received_calls, "GetSystemInfo was never called — client did not connect"
    # Verify that the SDK sends its standard gRPC metadata regardless of the
    # override_origin setting (regression guard).
    meta = received_calls[0]["metadata"]
    assert "client-name" in meta
    assert meta["client-name"] == "temporal-python"


async def test_override_origin_invalid_uri_raises():
    """An unparseable override_origin URI raises ValueError before connecting."""
    config = temporalio.service.ConnectConfig(
        target_host="localhost:7233",
        override_origin="this is not\na valid uri",
    )
    with pytest.raises(Exception, match="invalid override_origin"):
        await temporalio.service.ServiceClient.connect(config)


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
