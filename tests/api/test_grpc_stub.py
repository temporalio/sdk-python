import logging

import grpc

import temporalio
import temporalio.api.workflowservice.v1


class SimpleServer(temporalio.api.workflowservice.v1.WorkflowServiceServicer):
    async def CountWorkflowExecutions(
        self,
        request: temporalio.api.workflowservice.v1.CountWorkflowExecutionsRequest,
        context: grpc.aio.ServicerContext,
    ) -> temporalio.api.workflowservice.v1.CountWorkflowExecutionsResponse:
        logging.info("Server RPC called")
        assert request.namespace == "my namespace"
        assert request.query == "my query"
        return temporalio.api.workflowservice.v1.CountWorkflowExecutionsResponse(
            count=123
        )


async def test_python_grpc_stub():
    """Make sure pure Python gRPC client works."""

    # Start server
    server = grpc.aio.server()
    temporalio.api.workflowservice.v1.add_WorkflowServiceServicer_to_server(
        SimpleServer(), server
    )
    port = server.add_insecure_port("[::]:0")
    logging.info("Starting server on %s", port)
    await server.start()

    async with grpc.aio.insecure_channel(f"localhost:{port}") as channel:
        stub = temporalio.api.workflowservice.v1.WorkflowServiceStub(channel)
        response = await stub.CountWorkflowExecutions(
            temporalio.api.workflowservice.v1.CountWorkflowExecutionsRequest(
                namespace="my namespace", query="my query"
            )
        )
        assert response.count == 123

    logging.info("Stopping server")
    await server.stop(grace=None)
