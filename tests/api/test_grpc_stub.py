from datetime import timedelta

from google.protobuf.empty_pb2 import Empty
from google.protobuf.timestamp_pb2 import Timestamp
from grpc.aio import ServicerContext
from grpc.aio import server as grpc_server

from temporalio.api.operatorservice.v1 import (
    DeleteNamespaceRequest,
    DeleteNamespaceResponse,
    OperatorServiceServicer,
    add_OperatorServiceServicer_to_server,
)
from temporalio.api.testservice.v1 import (
    GetCurrentTimeResponse,
    TestServiceServicer,
    add_TestServiceServicer_to_server,
)
from temporalio.api.workflowservice.v1 import (
    CountWorkflowExecutionsRequest,
    CountWorkflowExecutionsResponse,
    GetSystemInfoRequest,
    GetSystemInfoResponse,
    WorkflowServiceServicer,
    add_WorkflowServiceServicer_to_server,
)
from temporalio.client import Client


def assert_metadata(context: ServicerContext, **kwargs) -> None:
    metadata = dict(context.invocation_metadata())
    for k, v in kwargs.items():
        assert metadata.get(k) == v


def assert_time_remaining(context: ServicerContext, expected: int) -> None:
    # Give or take 5 seconds
    assert expected - 5 <= context.time_remaining() <= expected + 5


class SimpleWorkflowServer(WorkflowServiceServicer):
    async def GetSystemInfo(  # type: ignore # https://github.com/nipunn1313/mypy-protobuf/issues/216
        self,
        request: GetSystemInfoRequest,
        context: ServicerContext,
    ) -> GetSystemInfoResponse:
        assert_metadata(context, client_key="client_value")
        return GetSystemInfoResponse()

    async def CountWorkflowExecutions(  # type: ignore # https://github.com/nipunn1313/mypy-protobuf/issues/216
        self,
        request: CountWorkflowExecutionsRequest,
        context: ServicerContext,
    ) -> CountWorkflowExecutionsResponse:
        assert_metadata(context, client_key="client_value", rpc_key="rpc_value")
        assert_time_remaining(context, 123)
        assert request.namespace == "my namespace"
        assert request.query == "my query"
        return CountWorkflowExecutionsResponse(count=123)


class SimpleOperatorServer(OperatorServiceServicer):
    async def DeleteNamespace(  # type: ignore # https://github.com/nipunn1313/mypy-protobuf/issues/216
        self,
        request: DeleteNamespaceRequest,
        context: ServicerContext,
    ) -> DeleteNamespaceResponse:
        assert_metadata(context, client_key="client_value", rpc_key="rpc_value")
        assert_time_remaining(context, 123)
        assert request.namespace == "my namespace"
        return DeleteNamespaceResponse(deleted_namespace="my namespace response")


class SimpleTestServer(TestServiceServicer):
    async def GetCurrentTime(  # type: ignore # https://github.com/nipunn1313/mypy-protobuf/issues/216
        self,
        request: Empty,
        context: ServicerContext,
    ) -> GetCurrentTimeResponse:
        assert_metadata(context, client_key="client_value", rpc_key="rpc_value")
        assert_time_remaining(context, 123)
        return GetCurrentTimeResponse(time=Timestamp(seconds=123))


async def test_python_grpc_stub():
    """Make sure pure Python gRPC client works."""

    # Start server
    server = grpc_server()
    add_WorkflowServiceServicer_to_server(SimpleWorkflowServer(), server)
    add_OperatorServiceServicer_to_server(SimpleOperatorServer(), server)
    add_TestServiceServicer_to_server(SimpleTestServer(), server)
    port = server.add_insecure_port("[::]:0")
    await server.start()

    # Use our client to make a call to each service
    client = await Client.connect(
        f"localhost:{port}", rpc_metadata={"client_key": "client_value"}
    )
    metadata = {"rpc_key": "rpc_value"}
    timeout = timedelta(seconds=123)
    count_resp = await client.workflow_service.count_workflow_executions(
        CountWorkflowExecutionsRequest(namespace="my namespace", query="my query"),
        metadata=metadata,
        timeout=timeout,
    )
    assert count_resp.count == 123
    del_resp = await client.operator_service.delete_namespace(
        DeleteNamespaceRequest(namespace="my namespace"),
        metadata=metadata,
        timeout=timeout,
    )
    assert del_resp.deleted_namespace == "my namespace response"
    time_resp = await client.test_service.get_current_time(
        Empty(), metadata=metadata, timeout=timeout
    )
    assert time_resp.time.seconds == 123
    await server.stop(grace=None)
