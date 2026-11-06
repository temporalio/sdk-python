import importlib
import pickle

import pytest


@pytest.mark.parametrize(
    ("module_name", "type_name"),
    [
        ("temporalio.api.common.v1.message_pb2", "Payload"),
        ("temporalio.bridge.proto.common.common_pb2", "NamespacedWorkflowExecution"),
        (
            "temporalio.api.dependencies.nexusannotations.v1.options_pb2",
            "OperationOptions",
        ),
        (
            "temporalio.api.dependencies.protoc_gen_openapiv2.options.openapiv2_pb2",
            "Info",
        ),
        (
            "tests.worker.workflow_sandbox.testmodules.proto.proto_message_pb2",
            "SomeMessage",
        ),
    ],
)
def test_generated_protobuf_pickle(module_name: str, type_name: str):
    module = importlib.import_module(module_name)
    message = getattr(module, type_name)()

    assert type(message).__module__ == module_name
    assert pickle.loads(pickle.dumps(message)) == message
