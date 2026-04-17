from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Sequence
from typing import Any, cast

import nexusrpc.handler
import pytest

import temporalio.api.common.v1
import temporalio.converter
import temporalio.nexus.system as nexus_system
from temporalio import workflow
from temporalio.client import Client
from temporalio.converter import (
    DefaultPayloadConverter,
    ExternalStorage,
    PayloadCodec,
)
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


@nexusrpc.handler.service_handler(service=generated.WorkflowService)
class WorkflowServicePayloadHandler:
    @nexusrpc.handler.sync_operation
    async def signal_with_start_workflow_execution(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        request: generated.SignalWithStartWorkflowExecutionRequest,
    ) -> generated.SignalWithStartWorkflowExecutionResponse:
        assert request.workflowId == "system-nexus-workflow-id"
        assert request.signalName == "test-signal"
        received_requests.append(dataclasses.asdict(request))
        return generated.SignalWithStartWorkflowExecutionResponse(
            runId=f"{request.workflowId}-run"
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
    assert payloads[0]["externalPayloads"]


def _assert_request_user_metadata_was_externally_stored(
    request_dict: dict[str, Any],
) -> None:
    user_metadata = cast(
        "dict[str, dict[str, object]] | None", request_dict.get("userMetadata")
    )
    assert user_metadata is not None
    assert user_metadata["summary"]["externalPayloads"]
    assert user_metadata["details"]["externalPayloads"]


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
    handler_config["data_converter"] = temporalio.converter.default()
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
        )

    assert result == "system-nexus-workflow-id-run"
    request_dict = _pop_received_request()
    _assert_request_payload_was_externally_stored(request_dict, "input")
    _assert_request_payload_was_externally_stored(request_dict, "signalInput")
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
