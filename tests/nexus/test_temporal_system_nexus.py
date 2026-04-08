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
from temporalio import workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import ExternalStorage, PayloadCodec
from temporalio.nexus.system import (
    WorkflowService,
    WorkflowServiceSignalWithStartWorkflowExecutionInput,
    WorkflowServiceSignalWithStartWorkflowExecutionOutput,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker._workflow_instance import UnsandboxedWorkflowRunner
from tests.helpers.nexus import make_nexus_endpoint_name
from tests.test_extstore import InMemoryTestDriver


@nexusrpc.handler.service_handler(service=WorkflowService)
class WorkflowServicePayloadHandler:
    @nexusrpc.handler.sync_operation
    async def signal_with_start_workflow_execution(
        self,
        _ctx: nexusrpc.handler.StartOperationContext,
        request: WorkflowServiceSignalWithStartWorkflowExecutionInput,
    ) -> WorkflowServiceSignalWithStartWorkflowExecutionOutput:
        request_dict = request.model_dump(by_alias=True)
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
        return WorkflowServiceSignalWithStartWorkflowExecutionOutput(
            runId=f"{request.workflow_id}-run"
        )


@workflow.defn
class SystemNexusCallerWithPayloadsWorkflow:
    @workflow.run
    async def run(self, task_queue: str) -> str:
        nexus_client = workflow.create_nexus_client(
            service=WorkflowService,
            endpoint=make_nexus_endpoint_name(task_queue),
        )
        request = WorkflowServiceSignalWithStartWorkflowExecutionInput.model_validate(
            {
                "namespace": "default",
                "workflowId": "system-nexus-workflow-id",
                "signalName": "test-signal",
                "input": MessageToDict(
                    temporalio.api.common.v1.Payloads(
                        payloads=[
                            temporalio.api.common.v1.Payload(
                                metadata={"encoding": b"json/plain"},
                                data=b'"workflow-input"',
                            )
                        ]
                    )
                ),
                "signalInput": MessageToDict(
                    temporalio.api.common.v1.Payloads(
                        payloads=[
                            temporalio.api.common.v1.Payload(
                                metadata={"encoding": b"json/plain"},
                                data=b'"signal-input"',
                            )
                        ]
                    )
                ),
                "memo": MessageToDict(
                    temporalio.api.common.v1.Memo(
                        fields={
                            "memo-key": temporalio.api.common.v1.Payload(
                                metadata={"encoding": b"json/plain"},
                                data=b'"memo-value"',
                            )
                        }
                    )
                ),
                "header": MessageToDict(
                    temporalio.api.common.v1.Header(
                        fields={
                            "header-key": temporalio.api.common.v1.Payload(
                                metadata={"encoding": b"json/plain"},
                                data=b'"header-value"',
                            )
                        }
                    )
                ),
                "userMetadata": {
                    "summary": MessageToDict(
                        temporalio.api.common.v1.Payload(
                            metadata={"encoding": b"json/plain"},
                            data=b'"summary-value"',
                        )
                    ),
                    "details": MessageToDict(
                        temporalio.api.common.v1.Payload(
                            metadata={"encoding": b"json/plain"},
                            data=b'"details-value"',
                        )
                    ),
                },
                "searchAttributes": {
                    "indexedFields": {
                        "custom-key": MessageToDict(
                            temporalio.api.common.v1.Payload(
                                metadata={"encoding": b"json/plain"},
                                data=b'"search-attribute-value"',
                            )
                        )
                    }
                },
            }
        )
        handle = await nexus_client.start_operation(
            WorkflowService.signal_with_start_workflow_execution,
            request,
        )
        result = await handle
        return cast(str, result.run_id)


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
        return list(payloads)


async def test_workflow_service_signal_with_start_nested_payloads_use_codec_without_encoding_outer_envelope(
    env: WorkflowEnvironment,
):
    if env.supports_time_skipping:
        pytest.skip("Nexus tests don't work with the Java test server")

    codec = RejectOuterSystemNexusCodec()
    driver = InMemoryTestDriver()
    config = env.client.config()
    config["data_converter"] = dataclasses.replace(
        pydantic_data_converter,
        payload_codec=codec,
        external_storage=ExternalStorage(
            drivers=[driver],
            payload_size_threshold=1,
        ),
    )
    client = Client(**config)

    async with Worker(
        client,
        task_queue=str(uuid.uuid4()),
        workflows=[SystemNexusCallerWithPayloadsWorkflow],
        nexus_service_handlers=[WorkflowServicePayloadHandler()],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ) as worker:
        endpoint_name = make_nexus_endpoint_name(worker.task_queue)
        await env.create_nexus_endpoint(endpoint_name, worker.task_queue)
        result = await client.execute_workflow(
            SystemNexusCallerWithPayloadsWorkflow.run,
            worker.task_queue,
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
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
