import google.protobuf.empty_pb2
import google.protobuf.timestamp_pb2
import grpc

import temporalio
import temporalio.api.operatorservice.v1
import temporalio.api.testservice.v1
import temporalio.api.workflowservice.v1


class SimpleWorkflowServer(temporalio.api.workflowservice.v1.WorkflowServiceServicer):
    async def CountWorkflowExecutions(  # type: ignore # https://github.com/nipunn1313/mypy-protobuf/issues/216
        self,
        request: temporalio.api.workflowservice.v1.CountWorkflowExecutionsRequest,
        context: grpc.aio.ServicerContext,
    ) -> temporalio.api.workflowservice.v1.CountWorkflowExecutionsResponse:
        assert request.namespace == "my namespace"
        assert request.query == "my query"
        return temporalio.api.workflowservice.v1.CountWorkflowExecutionsResponse(
            count=123
        )


class SimpleOperatorServer(temporalio.api.operatorservice.v1.OperatorServiceServicer):
    async def DeleteNamespace(  # type: ignore # https://github.com/nipunn1313/mypy-protobuf/issues/216
        self,
        request: temporalio.api.operatorservice.v1.DeleteNamespaceRequest,
        context: grpc.aio.ServicerContext,
    ) -> temporalio.api.operatorservice.v1.DeleteNamespaceResponse:
        assert request.namespace == "my namespace"
        return temporalio.api.operatorservice.v1.DeleteNamespaceResponse(
            deleted_namespace="my namespace response"
        )


class SimpleTestServer(temporalio.api.testservice.v1.TestServiceServicer):
    async def GetCurrentTime(  # type: ignore # https://github.com/nipunn1313/mypy-protobuf/issues/216
        self,
        request: google.protobuf.empty_pb2.Empty,
        context: grpc.aio.ServicerContext,
    ) -> temporalio.api.testservice.v1.GetCurrentTimeResponse:
        return temporalio.api.testservice.v1.GetCurrentTimeResponse(
            time=google.protobuf.timestamp_pb2.Timestamp(seconds=123)
        )


async def test_python_grpc_stub():
    """Make sure pure Python gRPC client works."""

    # Start server
    server = grpc.aio.server()
    temporalio.api.workflowservice.v1.add_WorkflowServiceServicer_to_server(
        SimpleWorkflowServer(), server
    )
    temporalio.api.operatorservice.v1.add_OperatorServiceServicer_to_server(
        SimpleOperatorServer(), server
    )
    temporalio.api.testservice.v1.add_TestServiceServicer_to_server(
        SimpleTestServer(), server
    )
    port = server.add_insecure_port("[::]:0")
    await server.start()

    async with grpc.aio.insecure_channel(f"localhost:{port}") as channel:
        response = await temporalio.api.workflowservice.v1.WorkflowServiceStub(
            channel
        ).CountWorkflowExecutions(
            temporalio.api.workflowservice.v1.CountWorkflowExecutionsRequest(
                namespace="my namespace", query="my query"
            )
        )
        assert response.count == 123
        response = await temporalio.api.operatorservice.v1.OperatorServiceStub(
            channel
        ).DeleteNamespace(
            temporalio.api.operatorservice.v1.DeleteNamespaceRequest(
                namespace="my namespace"
            )
        )
        assert response.deleted_namespace == "my namespace response"
        response = await temporalio.api.testservice.v1.TestServiceStub(
            channel
        ).GetCurrentTime(google.protobuf.empty_pb2.Empty())
        assert response.time.seconds == 123

    await server.stop(grace=None)
