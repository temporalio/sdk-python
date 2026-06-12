from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast

import pytest
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

import temporalio.api.common.v1
import temporalio.api.workflowservice.v1.request_response_pb2 as workflowservice_pb2
import temporalio.converter
import temporalio.nexus.system as nexus_system
from temporalio import workflow
from temporalio.client import Client
from temporalio.converter import ExternalStorage, PayloadCodec
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    Interceptor,
    StartNexusOperationInput,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)
from temporalio.worker._workflow_instance import UnsandboxedWorkflowRunner
from tests.test_extstore import InMemoryTestDriver

interceptor_traces: list[tuple[str, object]] = []


@workflow.defn
class ExternalHandleSignalWithStartWorkflowCaller:
    @workflow.run
    async def run(self, task_queue: str) -> str:
        started_handle = await workflow.signal_with_start_workflow(
            "test-workflow",
            "workflow-input",
            id="system-nexus-workflow-id",
            task_queue=task_queue,
            signal="test-signal",
            signal_args=["signal-input"],
            memo={"memo-key": "memo-value"},
            static_summary="summary-value",
            static_details="details-value",
        )
        return started_handle.id


class RejectOuterSystemNexusCodec(PayloadCodec):
    def __init__(self) -> None:
        self.encode_count = 0

    async def encode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        encoded: list[temporalio.api.common.v1.Payload] = []
        for payload in payloads:
            if (
                payload.metadata.get("encoding") == b"binary/protobuf"
                and payload.metadata.get("messageType")
                == b"temporal.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest"
            ):
                raise RuntimeError(
                    "outer system nexus envelope should not be codec encoded"
                )
            self.encode_count += 1
            encoded.append(
                temporalio.api.common.v1.Payload(
                    metadata={**payload.metadata, "test-codec": b"true"},
                    data=payload.data,
                )
            )
        return encoded

    async def decode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        decoded: list[temporalio.api.common.v1.Payload] = []
        for payload in payloads:
            if (
                payload.metadata.get("encoding") == b"binary/protobuf"
                and payload.metadata.get("messageType")
                == b"temporal.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest"
            ):
                raise RuntimeError(
                    "outer system nexus envelope should not be codec decoded"
                )
            decoded.append(payload)
        return decoded


class TracingWorkflowInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        return _TracingWorkflowInboundInterceptor


class _TracingWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_TracingWorkflowOutboundInterceptor(outbound))


class _TracingWorkflowOutboundInterceptor(WorkflowOutboundInterceptor):
    async def start_nexus_operation(
        self, input: StartNexusOperationInput[Any, Any]
    ) -> workflow.NexusOperationHandle[Any]:
        interceptor_traces.append(("workflow.start_nexus_operation", input))
        return await super().start_nexus_operation(input)


def _assert_stored_payloads_include(
    driver: InMemoryTestDriver, expected_payload_data: set[bytes]
) -> None:
    stored_payload_data: set[bytes] = set()
    for stored_payload_bytes in driver._storage.values():
        stored_payload = temporalio.api.common.v1.Payload()
        stored_payload.ParseFromString(stored_payload_bytes)
        assert stored_payload.metadata["test-codec"] == b"true"
        stored_payload_data.add(stored_payload.data)
    assert expected_payload_data.issubset(stored_payload_data)


def _assert_start_nexus_operation_interceptor_trace() -> None:
    assert len(interceptor_traces) == 1
    trace_name, trace_value = interceptor_traces.pop()
    assert trace_name == "workflow.start_nexus_operation"
    trace_input = cast(StartNexusOperationInput[Any, Any], trace_value)
    request = cast(
        workflowservice_pb2.SignalWithStartWorkflowExecutionRequest,
        trace_input.input,
    )
    assert request.workflow_id == "system-nexus-workflow-id"
    assert request.signal_name == "test-signal"
    assert request.workflow_type.name == "test-workflow"


def _build_proto_sample(message_type: type[Message]) -> Message:
    message = message_type()
    _populate_proto_sample(message)
    return message


def _populate_proto_sample(message: Message, *, path: str = "value") -> None:
    seen_oneofs: set[str] = set()
    for field in message.DESCRIPTOR.fields:
        if field.containing_oneof is not None:
            if field.containing_oneof.name in seen_oneofs:
                continue
            seen_oneofs.add(field.containing_oneof.name)
        if field.label == FieldDescriptor.LABEL_REPEATED:
            if (
                field.message_type is not None
                and field.message_type.GetOptions().map_entry
            ):
                _populate_proto_map_entry(message, field, path=path)
            elif field.cpp_type == FieldDescriptor.CPPTYPE_MESSAGE:
                _populate_proto_sample(
                    getattr(message, field.name).add(),
                    path=f"{path}.{field.name}[0]",
                )
            else:
                getattr(message, field.name).append(
                    _proto_scalar_sample(field, path=f"{path}.{field.name}[0]")
                )
        elif field.cpp_type == FieldDescriptor.CPPTYPE_MESSAGE:
            _populate_proto_sample(
                getattr(message, field.name),
                path=f"{path}.{field.name}",
            )
        else:
            setattr(
                message,
                field.name,
                _proto_scalar_sample(field, path=f"{path}.{field.name}"),
            )


def _populate_proto_map_entry(
    message: Message,
    field: FieldDescriptor,
    *,
    path: str,
) -> None:
    key_field = field.message_type.fields_by_name["key"]
    value_field = field.message_type.fields_by_name["value"]
    key = _proto_scalar_sample(key_field, path=f"{path}.{field.name}.key")
    container = getattr(message, field.name)
    if value_field.cpp_type == FieldDescriptor.CPPTYPE_MESSAGE:
        _populate_proto_sample(
            container[key],
            path=f"{path}.{field.name}[{key!r}]",
        )
    else:
        container[key] = _proto_scalar_sample(
            value_field,
            path=f"{path}.{field.name}[{key!r}]",
        )


def _proto_scalar_sample(field: FieldDescriptor, *, path: str) -> Any:
    if field.type == FieldDescriptor.TYPE_BYTES:
        return b"test"
    if field.cpp_type == FieldDescriptor.CPPTYPE_STRING:
        return f"{path}-value"
    if field.cpp_type == FieldDescriptor.CPPTYPE_BOOL:
        return True
    if field.cpp_type in (
        FieldDescriptor.CPPTYPE_INT32,
        FieldDescriptor.CPPTYPE_INT64,
        FieldDescriptor.CPPTYPE_UINT32,
        FieldDescriptor.CPPTYPE_UINT64,
    ):
        return 1
    if field.cpp_type in (
        FieldDescriptor.CPPTYPE_FLOAT,
        FieldDescriptor.CPPTYPE_DOUBLE,
    ):
        return 1.5
    if field.cpp_type == FieldDescriptor.CPPTYPE_ENUM:
        for enum_value in field.enum_type.values:
            if enum_value.number != 0:
                return enum_value.number
        return field.enum_type.values[0].number
    raise TypeError(f"Unhandled proto scalar sample at {path}: {field!r}")


@pytest.mark.parametrize(
    "message_type",
    [
        workflowservice_pb2.SignalWithStartWorkflowExecutionRequest,
        workflowservice_pb2.SignalWithStartWorkflowExecutionResponse,
    ],
)
def test_system_nexus_proto_roundtrip(message_type: type[Message]) -> None:
    payload_converter = nexus_system.get_payload_converter()
    proto_value = _build_proto_sample(message_type)
    payload = payload_converter.to_payload(proto_value)
    assert payload is not None
    assert payload.metadata["encoding"] == b"binary/protobuf"
    assert payload.metadata["messageType"] == message_type.DESCRIPTOR.full_name.encode()
    roundtripped = payload_converter.from_payload(payload, message_type)
    assert isinstance(roundtripped, message_type)
    assert roundtripped == proto_value


async def test_external_workflow_handle_signal_with_start_workflow_uses_system_nexus(
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the Java test server")

    codec = RejectOuterSystemNexusCodec()
    interceptor_traces.clear()
    driver = InMemoryTestDriver()
    caller_config = env.client.config()
    caller_config["data_converter"] = dataclasses.replace(
        temporalio.converter.default(),
        payload_codec=codec,
        external_storage=ExternalStorage(
            drivers=[driver],
            payload_size_threshold=1,
        ),
    )
    caller_client = Client(**caller_config)
    caller_task_queue = str(uuid.uuid4())
    handler_task_queue = str(uuid.uuid4())

    caller_worker = Worker(
        caller_client,
        task_queue=caller_task_queue,
        workflows=[ExternalHandleSignalWithStartWorkflowCaller],
        workflow_runner=UnsandboxedWorkflowRunner(),
        interceptors=[TracingWorkflowInterceptor()],
    )

    async with caller_worker:
        result = await caller_client.execute_workflow(
            ExternalHandleSignalWithStartWorkflowCaller.run,
            args=[handler_task_queue],
            id=str(uuid.uuid4()),
            task_queue=caller_task_queue,
            execution_timeout=timedelta(seconds=5),
        )

    assert result == "system-nexus-workflow-id"
    assert codec.encode_count >= 5
    _assert_stored_payloads_include(
        driver,
        {
            b'"workflow-input"',
            b'"signal-input"',
            b'"memo-value"',
            b'"summary-value"',
            b'"details-value"',
        },
    )
    _assert_start_nexus_operation_interceptor_trace()
