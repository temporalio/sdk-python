import inspect
import re
from typing import Dict, Tuple, Type

import grpc

import temporalio.api.workflowservice.v1
import temporalio.workflow_service
from temporalio.client import Client


def test_load_default_worker_binary_id():
    # Just run it twice and confirm it didn't change
    val1 = temporalio.workflow_service.load_default_worker_binary_id(memoize=False)
    val2 = temporalio.workflow_service.load_default_worker_binary_id(memoize=False)
    assert val1 == val2


def test_all_grpc_calls_present(client: Client):
    # Collect workflow service calls
    workflow_service_calls: Dict[str, Tuple[Type, Type]] = {}
    for member in inspect.getmembers(client.service):
        _, call = member
        if isinstance(call, temporalio.workflow_service.WorkflowServiceCall):
            workflow_service_calls[call.name] = (call.req_type, call.resp_type)

    # Collect gRPC service calls with a fake channel
    channel = CallCollectingChannel()
    temporalio.api.workflowservice.v1.WorkflowServiceStub(channel)
    # TODO(cretz): Remove once https://github.com/temporalio/sdk-core/issues/335 fixed
    del channel.calls["get_workflow_execution_history_reverse"]

    assert channel.calls == workflow_service_calls


class CallCollectingChannel(grpc.Channel):
    def __init__(self) -> None:
        super().__init__()
        self.calls: Dict[str, Tuple[Type, Type]] = {}

    def unary_unary(self, method, request_serializer, response_deserializer):
        # Last part after slash
        name = method.rsplit("/", 1)[-1]
        req = getattr(temporalio.api.workflowservice.v1, name + "Request")
        resp = getattr(temporalio.api.workflowservice.v1, name + "Response")
        # Camel to snake case
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        self.calls[name] = (req, resp)


CallCollectingChannel.__abstractmethods__ = set()
