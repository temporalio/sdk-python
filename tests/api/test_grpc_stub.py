import re
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Union, cast

import pytest
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


def assert_time_remaining(context: ServicerContext, expected: int) -> None:
    # Give or take 5 seconds
    assert expected - 5 <= context.time_remaining() <= expected + 5


class SimpleWorkflowServer(WorkflowServiceServicer):
    def __init__(self) -> None:
        super().__init__()
        self.last_metadata: Mapping[str, str | bytes] = {}

    def assert_last_metadata(self, expected: Mapping[str, str | bytes]) -> None:
        for k, v in expected.items():
            assert self.last_metadata.get(k) == v

    async def GetSystemInfo(  # type: ignore # https://github.com/nipunn1313/mypy-protobuf/issues/216
        self,
        request: GetSystemInfoRequest,
        context: ServicerContext,
    ) -> GetSystemInfoResponse:
        self.last_metadata = dict(context.invocation_metadata())
        return GetSystemInfoResponse()

    async def CountWorkflowExecutions(  # type: ignore # https://github.com/nipunn1313/mypy-protobuf/issues/216
        self,
        request: CountWorkflowExecutionsRequest,
        context: ServicerContext,
    ) -> CountWorkflowExecutionsResponse:
        self.last_metadata = dict(context.invocation_metadata())
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
        assert_time_remaining(context, 123)
        assert request.namespace == "my namespace"
        return DeleteNamespaceResponse(deleted_namespace="my namespace response")


class SimpleTestServer(TestServiceServicer):
    async def GetCurrentTime(  # type: ignore # https://github.com/nipunn1313/mypy-protobuf/issues/216
        self,
        request: Empty,
        context: ServicerContext,
    ) -> GetCurrentTimeResponse:
        assert_time_remaining(context, 123)
        return GetCurrentTimeResponse(time=Timestamp(seconds=123))


async def test_python_grpc_stub():
    """Make sure pure Python gRPC client works."""

    # Start server
    server = grpc_server()
    workflow_server = SimpleWorkflowServer()  # type: ignore[abstract]
    add_WorkflowServiceServicer_to_server(workflow_server, server)
    add_OperatorServiceServicer_to_server(SimpleOperatorServer(), server)  # type: ignore[abstract]
    add_TestServiceServicer_to_server(SimpleTestServer(), server)  # type: ignore[abstract]
    port = server.add_insecure_port("[::]:0")
    await server.start()

    # Use our client to make a call to each service
    client = await Client.connect(f"localhost:{port}")
    timeout = timedelta(seconds=123)
    count_resp = await client.workflow_service.count_workflow_executions(
        CountWorkflowExecutionsRequest(namespace="my namespace", query="my query"),
        timeout=timeout,
    )
    assert count_resp.count == 123
    del_resp = await client.operator_service.delete_namespace(
        DeleteNamespaceRequest(namespace="my namespace"),
        timeout=timeout,
    )
    assert del_resp.deleted_namespace == "my namespace response"
    time_resp = await client.test_service.get_current_time(Empty(), timeout=timeout)
    assert time_resp.time.seconds == 123

    await server.stop(grace=None)


async def test_grpc_metadata():
    # Start server
    server = grpc_server()
    workflow_server = SimpleWorkflowServer()  # type: ignore[abstract]
    add_WorkflowServiceServicer_to_server(workflow_server, server)
    port = server.add_insecure_port("[::]:0")
    await server.start()

    # Connect and confirm metadata of get system info call
    client = await Client.connect(
        f"localhost:{port}",
        api_key="my-api-key",
        rpc_metadata={"my-meta-key": "my-meta-val"},
        tls=False,
    )
    workflow_server.assert_last_metadata(
        {
            "authorization": "Bearer my-api-key",
            "my-meta-key": "my-meta-val",
        }
    )

    # Binary metadata values should work:
    await client.workflow_service.get_system_info(
        GetSystemInfoRequest(),
        metadata={
            "my-binary-key-bin": b"\x00\x01",
        },
    )
    workflow_server.assert_last_metadata(
        {
            "authorization": "Bearer my-api-key",
            "my-meta-key": "my-meta-val",
            "my-binary-key-bin": b"\x00\x01",
        }
    )

    # Binary metadata should be configurable on the client:
    client.rpc_metadata = {
        "my-binary-key-bin": b"\x00\x01",
        "my-binary-key2-bin": b"\x02\x03",
    }
    await client.workflow_service.get_system_info(
        GetSystemInfoRequest(),
        metadata={
            "my-binary-key-bin": b"abc",
        },
    )
    workflow_server.assert_last_metadata(
        {
            "authorization": "Bearer my-api-key",
            "my-binary-key-bin": b"abc",
            "my-binary-key2-bin": b"\x02\x03",
        }
    )

    # Setting invalid RPC metadata should raise:
    with pytest.raises(
        ValueError,
        match="Invalid binary header key 'my-ascii-key': invalid gRPC metadata key name",
    ):
        client.rpc_metadata = {
            "my-ascii-key": b"binary-value",
        }
    with pytest.raises(
        ValueError,
        match="Invalid ASCII header key 'my-binary-key-bin': invalid gRPC metadata key name",
    ):
        client.rpc_metadata = {
            "my-binary-key-bin": "ascii-value",
        }

    # Making a request with invalid RPC metadata should raise:
    with pytest.raises(
        ValueError,
        match="Invalid metadata value for ASCII key my-ascii-key: expected str",
    ):
        await client.workflow_service.get_system_info(
            GetSystemInfoRequest(),
            metadata={
                "my-ascii-key": b"binary-value",
            },
        )
    with pytest.raises(
        ValueError,
        match="Invalid metadata value for binary key my-binary-key-bin: expected bytes",
    ):
        await client.workflow_service.get_system_info(
            GetSystemInfoRequest(),
            metadata={
                "my-binary-key-bin": "ascii-value",
            },
        )

    # Passing in non-`str | bytes` should raise:
    with pytest.raises(TypeError) as err:
        await client.workflow_service.get_system_info(
            GetSystemInfoRequest(),
            metadata={
                # Not a valid header:
                "my-int-key": cast(Any, 256),
            },
        )
    cause = err.value.__cause__
    assert isinstance(cause, TypeError)
    assert re.match(
        re.escape(r"failed to extract enum RpcMetadataValue ('str | bytes')"),
        str(cause),
    )
    with pytest.raises(
        TypeError,
        match=re.escape(r"failed to extract enum RpcMetadataValue ('str | bytes')"),
    ) as err:
        client.rpc_metadata = {
            "my-binary-key-bin": cast(Any, 256),
        }

    # Setting invalid RPC metadata in a mixed client will partially fail:
    client.rpc_metadata = {
        "x-my-binary-bin": b"\x00",
        "x-my-ascii": "foo",
    }
    assert client.rpc_metadata == {
        "x-my-binary-bin": b"\x00",
        "x-my-ascii": "foo",
    }
    with pytest.raises(
        ValueError,
        match="Invalid binary header key 'x-invalid-ascii-with-bin-value': invalid gRPC metadata key name",
    ):
        client.rpc_metadata = {
            "x-invalid-ascii-with-bin-value": b"not-ascii",
            "x-my-ascii": "bar",
        }
    assert client.rpc_metadata == {
        "x-my-binary-bin": b"\x00",
        "x-my-ascii": "foo",
    }
    await client.workflow_service.get_system_info(GetSystemInfoRequest())
    workflow_server.assert_last_metadata(
        {
            "authorization": "Bearer my-api-key",
            # This is inconsistent with what `client.rpc_metadata` returns
            # (`x-my-ascii` was updated):
            "x-my-binary-bin": b"\x00",
            "x-my-ascii": "bar",
        }
    )

    # Overwrite API key via client RPC metadata, confirm there
    client.rpc_metadata = {
        "authorization": "my-auth-val1",
        "my-meta-key": "my-meta-val",
    }
    await client.workflow_service.get_system_info(GetSystemInfoRequest())
    workflow_server.assert_last_metadata(
        {
            "authorization": "my-auth-val1",
            "my-meta-key": "my-meta-val",
        }
    )
    client.rpc_metadata = {"my-meta-key": "my-meta-val"}

    # Overwrite API key via call RPC metadata, confirm there
    await client.workflow_service.get_system_info(
        GetSystemInfoRequest(), metadata={"authorization": "my-auth-val2"}
    )
    workflow_server.assert_last_metadata(
        {
            "authorization": "my-auth-val2",
            "my-meta-key": "my-meta-val",
        }
    )

    # Update API key, confirm updated
    client.api_key = "my-new-api-key"
    await client.workflow_service.get_system_info(GetSystemInfoRequest())
    workflow_server.assert_last_metadata(
        {
            "authorization": "Bearer my-new-api-key",
            "my-meta-key": "my-meta-val",
        }
    )

    # Remove API key, confirm removed
    client.api_key = None
    await client.workflow_service.get_system_info(GetSystemInfoRequest())
    workflow_server.assert_last_metadata(
        {
            "my-meta-key": "my-meta-val",
        }
    )
    assert "authorization" not in workflow_server.last_metadata

    await server.stop(grace=None)
