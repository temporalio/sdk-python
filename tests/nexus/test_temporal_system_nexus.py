from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Sequence
from typing import cast

import nexusrpc.handler
import pytest
from google.protobuf.json_format import MessageToDict

import temporalio.api.common.v1
import temporalio.converter
from temporalio import workflow
from temporalio.client import Client
from temporalio.converter import (
    DefaultPayloadConverter,
    ExternalStorage,
    PayloadCodec,
)
from temporalio.nexus.system import generated
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker._workflow_instance import UnsandboxedWorkflowRunner
from tests.helpers.nexus import make_nexus_endpoint_name
from tests.test_extstore import InMemoryTestDriver


@nexusrpc.handler.service_handler(service=generated.WorkflowService)
class WorkflowServicePayloadHandler:
    @nexusrpc.handler.sync_operation
    async def signal_with_start_workflow_execution(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        request: generated.WorkflowServiceSignalWithStartWorkflowExecutionInput,
    ) -> generated.WorkflowServiceSignalWithStartWorkflowExecutionOutput:
        assert request.workflowId == "system-nexus-workflow-id"
        assert request.signalName == "test-signal"
        request_dict = dataclasses.asdict(request)
        for field_name in ("input", "signalInput"):
            payloads = request_dict[field_name]["payloads"]
            assert payloads[0]["externalPayloads"]
        for field_name in ("memo", "header"):
            fields = request_dict[field_name]["fields"]
            assert next(iter(fields.values()))["externalPayloads"]
        for field_name in ("summary", "details"):
            payload = request_dict["userMetadata"][field_name]
            assert payload["externalPayloads"]
        search_attribute_payload = request_dict["searchAttributes"]["indexedFields"][
            "custom-key"
        ]
        assert "externalPayloads" not in search_attribute_payload
        assert "test-codec" not in search_attribute_payload["metadata"]
        return generated.WorkflowServiceSignalWithStartWorkflowExecutionOutput(
            runId=f"{request.workflowId}-run"
        )


@workflow.defn
class SystemNexusCallerWithPayloadsWorkflow:
    @workflow.run
    async def run(self, task_queue: str) -> str:
        nexus_client = workflow.create_nexus_client(
            service=generated.WorkflowService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        request = generated.WorkflowServiceSignalWithStartWorkflowExecutionInput(
            namespace="default",
            workflowId="system-nexus-workflow-id",
            signalName="test-signal",
            input=generated.Input(
                payloads=[
                    MessageToDict(
                        temporalio.api.common.v1.Payload(
                            metadata={"encoding": b"json/plain"},
                            data=b'"workflow-input"',
                        )
                    )
                ]
            ),
            signalInput=generated.Input(
                payloads=[
                    MessageToDict(
                        temporalio.api.common.v1.Payload(
                            metadata={"encoding": b"json/plain"},
                            data=b'"signal-input"',
                        )
                    )
                ]
            ),
            memo=generated.Memo(
                fields={
                    "memo-key": MessageToDict(
                        temporalio.api.common.v1.Payload(
                            metadata={"encoding": b"json/plain"},
                            data=b'"memo-value"',
                        )
                    )
                }
            ),
            header=generated.Header(
                fields={
                    "header-key": MessageToDict(
                        temporalio.api.common.v1.Payload(
                            metadata={"encoding": b"json/plain"},
                            data=b'"header-value"',
                        )
                    )
                }
            ),
            userMetadata=generated.UserMetadata(
                summary=MessageToDict(
                    temporalio.api.common.v1.Payload(
                        metadata={"encoding": b"json/plain"},
                        data=b'"summary-value"',
                    )
                ),
                details=MessageToDict(
                    temporalio.api.common.v1.Payload(
                        metadata={"encoding": b"json/plain"},
                        data=b'"details-value"',
                    )
                ),
            ),
            searchAttributes=generated.SearchAttributes(
                indexedFields={
                    "custom-key": MessageToDict(
                        temporalio.api.common.v1.Payload(
                            metadata={"encoding": b"json/plain"},
                            data=b'"search-attribute-value"',
                        )
                    )
                }
            ),
        )
        handle = await nexus_client.start_operation(
            generated.WorkflowService.signal_with_start_workflow_execution,
            request,
        )
        result = await handle
        return cast(str, result.runId)


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
            if isinstance(
                value, generated.WorkflowServiceSignalWithStartWorkflowExecutionInput
            ):
                payloads.append(
                    temporalio.api.common.v1.Payload(
                        metadata={"encoding": b"json/plain"},
                        data=b'{"workflow_id":"bad-envelope"}',
                    )
                )
            else:
                payloads.extend(super().to_payloads([value]))
        return payloads


async def test_workflow_service_signal_with_start_nested_payloads_use_codec_without_encoding_outer_envelope(
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the Java test server")

    codec = RejectOuterSystemNexusCodec()
    driver = InMemoryTestDriver()
    caller_config = env.client.config()
    caller_config["data_converter"] = dataclasses.replace(
        temporalio.converter.default(),
        payload_converter_class=BadSystemNexusEnvelopePayloadConverter,
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

    caller_worker = Worker(
        caller_client,
        task_queue=caller_task_queue,
        workflows=[SystemNexusCallerWithPayloadsWorkflow],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    handler_worker = Worker(
        handler_client,
        task_queue=handler_task_queue,
        nexus_service_handlers=[WorkflowServicePayloadHandler()],
    )

    async with caller_worker, handler_worker:
        endpoint_name = make_nexus_endpoint_name(handler_task_queue)
        await env.create_nexus_endpoint(endpoint_name, handler_task_queue)
        result = await caller_client.execute_workflow(
            SystemNexusCallerWithPayloadsWorkflow.run,
            handler_task_queue,
            id=str(uuid.uuid4()),
            task_queue=caller_task_queue,
        )

    assert result == "system-nexus-workflow-id-run"
    assert codec.encode_count >= 6
    stored_payloads: list[temporalio.api.common.v1.Payload] = []
    for stored_payload_bytes in driver._storage.values():
        stored_payload = temporalio.api.common.v1.Payload()
        stored_payload.ParseFromString(stored_payload_bytes)
        stored_payloads.append(stored_payload)
        assert stored_payload.metadata["test-codec"] == b"true"
    stored_payload_data = {payload.data for payload in stored_payloads}
    assert {
        b'"workflow-input"',
        b'"signal-input"',
        b'"memo-value"',
        b'"header-value"',
        b'"summary-value"',
        b'"details-value"',
    }.issubset(stored_payload_data)
