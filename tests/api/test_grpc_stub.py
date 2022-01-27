import logging
from concurrent import futures

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
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    server = grpc.aio.server()
    temporalio.api.workflowservice.v1.add_WorkflowServiceServicer_to_server(
        SimpleServer(), server
    )
    listen_addr = "[::]:50051"
    server.add_insecure_port(listen_addr)

    logging.info("Starting server on %s", listen_addr)
    await server.start()

    async with grpc.aio.insecure_channel("localhost:50051") as channel:
        stub = temporalio.api.workflowservice.v1.WorkflowServiceStub(channel)
        response = await stub.CountWorkflowExecutions(
            temporalio.api.workflowservice.v1.CountWorkflowExecutionsRequest(
                namespace="my namespace", query="my query"
            )
        )
        assert response.count == 123

    logging.info("Stopping server")
    await server.stop(grace=None)
