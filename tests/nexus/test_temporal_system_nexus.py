from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, ClassVar, Protocol, cast

import nexusrpc.handler
import pytest
from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

import temporalio.api.common.v1
import temporalio.converter
import temporalio.nexus.system as nexus_system
from temporalio import workflow
from temporalio.client import Client
from temporalio.converter import DefaultPayloadConverter, ExternalStorage, PayloadCodec
from temporalio.nexus.system import generated
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import (
    Interceptor,
    SignalWithStartExternalWorkflowInput,
    Worker,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)
from temporalio.worker._workflow_instance import UnsandboxedWorkflowRunner
from tests.helpers.nexus import make_nexus_endpoint_name
from tests.test_extstore import InMemoryTestDriver

interceptor_traces: list[tuple[str, object]] = []
received_requests: list[dict[str, Any]] = []


class _AnnotatedSystemNexusMessage(Protocol):
    __temporal_nexus_proto_type__: ClassVar[type[Message]]

    @property
    def proto_type(self) -> type[Message]: ...


@nexusrpc.handler.service_handler(service=generated.WorkflowService)
class WorkflowServicePayloadHandler:
    @nexusrpc.handler.sync_operation
    async def signal_with_start_workflow_execution(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        request: generated.SignalWithStartWorkflowExecutionRequest,
    ) -> generated.SignalWithStartWorkflowExecutionResponse:
        assert request.workflow_id == "system-nexus-workflow-id"
        assert request.signal_name == "test-signal"
        received_requests.append(dataclasses.asdict(request))
        return generated.SignalWithStartWorkflowExecutionResponse(
            run_id=f"{request.workflow_id}-run"
        )


@workflow.defn
class ExternalHandleSignalWithStartWorkflowCaller:
    @workflow.run
    async def run(self, task_queue: str) -> str:
        handle = workflow.get_external_workflow_handle("system-nexus-workflow-id")
        started_handle = await handle.signal_with_start(
            "test-signal",
            "test-workflow",
            signal_args=["signal-input"],
            workflow_args=["workflow-input"],
            task_queue=task_queue,
            memo={"memo-key": "memo-value"},
            static_summary="summary-value",
            static_details="details-value",
        )
        return cast(str, started_handle.run_id)


class RejectOuterSystemNexusCodec(PayloadCodec):
    def __init__(self) -> None:
        self.encode_count = 0

    async def encode(
        self, payloads: Sequence[temporalio.api.common.v1.Payload]
    ) -> list[temporalio.api.common.v1.Payload]:
        encoded: list[temporalio.api.common.v1.Payload] = []
        for payload in payloads:
            try:
                body = json.loads(payload.data)
            except json.JSONDecodeError:
                body = None
            if isinstance(body, dict) and {
                "namespace",
                "workflowId",
                "signalName",
            }.issubset(body):
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
            try:
                body = json.loads(payload.data)
            except json.JSONDecodeError:
                body = None
            if isinstance(body, dict) and {
                "namespace",
                "workflowId",
                "signalName",
            }.issubset(body):
                raise RuntimeError(
                    "outer system nexus envelope should not be codec decoded"
                )
            decoded.append(payload)
        return decoded


class BadSystemNexusEnvelopePayloadConverter(DefaultPayloadConverter):
    def to_payloads(
        self, values: Sequence[object]
    ) -> list[temporalio.api.common.v1.Payload]:
        payloads: list[temporalio.api.common.v1.Payload] = []
        for value in values:
            if isinstance(value, generated.SignalWithStartWorkflowExecutionRequest):
                payloads.append(
                    temporalio.api.common.v1.Payload(
                        metadata={"encoding": b"json/plain"},
                        data=b'{"workflow_id":"bad-envelope"}',
                    )
                )
            else:
                payloads.extend(super().to_payloads([value]))
        return payloads


class TracingWorkflowInterceptor(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        return _TracingWorkflowInboundInterceptor


class _TracingWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_TracingWorkflowOutboundInterceptor(outbound))


class _TracingWorkflowOutboundInterceptor(WorkflowOutboundInterceptor):
    async def signal_with_start_external_workflow(
        self, input: SignalWithStartExternalWorkflowInput
    ) -> workflow.ExternalWorkflowHandle[object]:
        interceptor_traces.append(
            ("workflow.signal_with_start_external_workflow", input)
        )
        return await super().signal_with_start_external_workflow(input)


def _pop_received_request() -> dict[str, Any]:
    assert len(received_requests) == 1
    return received_requests.pop()


def _assert_request_payload_was_externally_stored(
    request_dict: dict[str, Any], field_name: str
) -> None:
    payloads = cast("dict[str, list[dict[str, object]]]", request_dict[field_name])[
        "payloads"
    ]
    assert len(payloads) == 1
    assert payloads[0]["external_payloads"]


def _assert_request_user_metadata_was_externally_stored(
    request_dict: dict[str, Any],
) -> None:
    user_metadata = cast(
        "dict[str, dict[str, object]] | None", request_dict.get("user_metadata")
    )
    assert user_metadata is not None
    assert user_metadata["summary"]["external_payloads"]
    assert user_metadata["details"]["external_payloads"]


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


def _assert_signal_with_start_interceptor_trace() -> None:
    assert len(interceptor_traces) == 1
    trace_name, trace_value = interceptor_traces.pop()
    assert trace_name == "workflow.signal_with_start_external_workflow"
    trace_input = cast(SignalWithStartExternalWorkflowInput, trace_value)
    assert trace_input.workflow_id == "system-nexus-workflow-id"
    assert trace_input.signal == "test-signal"
    assert trace_input.workflow == "test-workflow"


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


def test_generated_system_nexus_proto_roundtrip() -> None:
    payload_converter = nexus_system.get_payload_converter()
    annotated_types = sorted(
        (
            value
            for value in vars(generated).values()
            if isinstance(value, type)
            and dataclasses.is_dataclass(value)
            and hasattr(value, "__temporal_nexus_proto_type__")
        ),
        key=lambda value: value.__name__,
    )
    assert annotated_types

    for annotated_type in annotated_types:
        annotated_message_type = cast(
            type[_AnnotatedSystemNexusMessage], annotated_type
        )
        proto_type = annotated_message_type.__temporal_nexus_proto_type__
        proto_value = _build_proto_sample(proto_type)
        payload = payload_converter.to_payload(proto_value)
        assert payload is not None
        assert (
            payload.metadata["messageType"] == proto_type.DESCRIPTOR.full_name.encode()
        )
        value = payload_converter.from_payload(payload, annotated_message_type)
        assert dataclasses.is_dataclass(value)
        assert value.proto_type is proto_type
        roundtripped_payload = payload_converter.to_payload(value)
        assert roundtripped_payload is not None
        roundtripped = payload_converter.from_payload(
            roundtripped_payload, annotated_message_type
        )
        assert roundtripped == value


async def test_external_workflow_handle_signal_with_start_workflow_uses_system_nexus(
    env: WorkflowEnvironment,
    monkeypatch: pytest.MonkeyPatch,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the Java test server")

    codec = RejectOuterSystemNexusCodec()
    interceptor_traces.clear()
    received_requests.clear()
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
    handler_config = env.client.config()
    handler_config["data_converter"] = dataclasses.replace(
        temporalio.converter.default(),
        payload_converter_class=nexus_system.SystemNexusPayloadConverter,
    )
    handler_client = Client(**handler_config)
    caller_task_queue = str(uuid.uuid4())
    handler_task_queue = str(uuid.uuid4())
    endpoint_name = make_nexus_endpoint_name(handler_task_queue)
    monkeypatch.setattr(workflow, "_SYSTEM_NEXUS_ENDPOINT", endpoint_name)

    caller_worker = Worker(
        caller_client,
        task_queue=caller_task_queue,
        workflows=[ExternalHandleSignalWithStartWorkflowCaller],
        workflow_runner=UnsandboxedWorkflowRunner(),
        interceptors=[TracingWorkflowInterceptor()],
    )
    handler_worker = Worker(
        handler_client,
        task_queue=handler_task_queue,
        nexus_service_handlers=[WorkflowServicePayloadHandler()],
    )

    async with caller_worker, handler_worker:
        await env.create_nexus_endpoint(endpoint_name, handler_task_queue)
        result = await caller_client.execute_workflow(
            ExternalHandleSignalWithStartWorkflowCaller.run,
            args=[handler_task_queue],
            id=str(uuid.uuid4()),
            task_queue=caller_task_queue,
            execution_timeout=timedelta(seconds=5),
        )

    assert result == "system-nexus-workflow-id-run"
    request_dict = _pop_received_request()
    _assert_request_payload_was_externally_stored(request_dict, "input")
    _assert_request_payload_was_externally_stored(request_dict, "signal_input")
    _assert_request_user_metadata_was_externally_stored(request_dict)
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
    _assert_signal_with_start_interceptor_trace()
