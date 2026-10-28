"""Client paths encode and decode external storage reference payloads.

These use a fake workflow service, so they check exactly what the client sends
and how it reads responses without depending on a server.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.failure.v1
import temporalio.api.history.v1
import temporalio.api.update.v1
import temporalio.api.workflowservice.v1
import temporalio.client
import temporalio.converter
from temporalio.api.common.v1 import Payload
from temporalio.api.sdk.v1.external_storage_pb2 import ExternalStorageReference
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleSpec,
    WorkflowFailureError,
)
from temporalio.common import HeaderCodecBehavior
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
    JSONProtoPayloadConverter,
)
from temporalio.exceptions import ApplicationError
from temporalio.service import ServiceClient
from tests.test_extstore import InMemoryTestDriver, RecordingPayloadCodec

pytestmark = pytest.mark.usefixtures("encode_storage_references")

# Larger than the storage threshold below, so it is always stored externally.
LARGE = "x" * 200
CODEC_ENCODING = b"binary/test-encoded"


class _FakeWorkflowService:
    """Records each request and returns the configured response."""

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.requests: dict[str, Any] = {}
        for name, response in responses.items():
            setattr(self, name, self._recorder(name, response))

    def _recorder(self, name: str, response: Any) -> AsyncMock:
        async def record(req: Any, **_kwargs: Any) -> Any:
            self.requests[name] = req
            return response

        return AsyncMock(side_effect=record)


def _data_converter() -> DataConverter:
    return DataConverter(
        payload_codec=RecordingPayloadCodec(CODEC_ENCODING.decode()),
        external_storage=ExternalStorage(
            drivers=[InMemoryTestDriver()], payload_size_threshold=50
        ),
    )


def _client(
    service: _FakeWorkflowService,
    data_converter: DataConverter,
    **kwargs: Any,
) -> Client:
    service_client = Mock(spec=ServiceClient)
    service_client.workflow_service = service
    service_client.config = Mock(identity="test-identity")
    return Client(
        service_client=service_client,
        namespace="test-namespace",
        data_converter=data_converter,
        **kwargs,
    )


async def _assert_encoded_reference(
    data_converter: DataConverter, payload: Payload
) -> None:
    assert payload.metadata["encoding"] == CODEC_ENCODING
    assert payload.metadata["__temporal_external_storage_reference"] == b"true"
    assert payload.external_payloads
    [reference] = await data_converter._decode_reference_payload_sequence([payload])
    assert isinstance(
        JSONProtoPayloadConverter().from_payload(reference, ExternalStorageReference),
        ExternalStorageReference,
    )


async def _encoded_reference(data_converter: DataConverter, value: Any) -> Payload:
    [payload] = await data_converter.encode([value])
    await _assert_encoded_reference(data_converter, payload)
    return payload


def _history_response(
    event: temporalio.api.history.v1.HistoryEvent,
) -> temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryResponse:
    return temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryResponse(
        history=temporalio.api.history.v1.History(events=[event])
    )


async def test_start_workflow_encodes_input_and_memo_references() -> None:
    service = _FakeWorkflowService(
        {
            "start_workflow_execution": temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse(
                run_id="run"
            )
        }
    )
    client = _client(service, _data_converter())

    await client.start_workflow(
        "Workflow", LARGE, id="wf", task_queue="tq", memo={"key": LARGE}
    )

    req = service.requests["start_workflow_execution"]
    await _assert_encoded_reference(client.data_converter, req.input.payloads[0])
    await _assert_encoded_reference(client.data_converter, req.memo.fields["key"])


async def test_signal_encodes_input_reference() -> None:
    service = _FakeWorkflowService(
        {
            "signal_workflow_execution": temporalio.api.workflowservice.v1.SignalWorkflowExecutionResponse()
        }
    )
    client = _client(service, _data_converter())

    await client.get_workflow_handle("wf").signal("signal", LARGE)

    await _assert_encoded_reference(
        client.data_converter,
        service.requests["signal_workflow_execution"].input.payloads[0],
    )


async def test_signal_with_start_encodes_input_references() -> None:
    service = _FakeWorkflowService(
        {
            "signal_with_start_workflow_execution": temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionResponse(
                run_id="run"
            )
        }
    )
    client = _client(service, _data_converter())

    await client.start_workflow(
        "Workflow",
        LARGE,
        id="wf",
        task_queue="tq",
        start_signal="signal",
        start_signal_args=[LARGE],
    )

    req = service.requests["signal_with_start_workflow_execution"]
    await _assert_encoded_reference(client.data_converter, req.input.payloads[0])
    await _assert_encoded_reference(client.data_converter, req.signal_input.payloads[0])


async def test_update_encodes_args_and_decodes_result_references() -> None:
    data_converter = _data_converter()
    result = await _encoded_reference(data_converter, "update-result" * 20)
    service = _FakeWorkflowService(
        {
            "update_workflow_execution": temporalio.api.workflowservice.v1.UpdateWorkflowExecutionResponse(
                stage=temporalio.api.enums.v1.UpdateWorkflowExecutionLifecycleStage.UPDATE_WORKFLOW_EXECUTION_LIFECYCLE_STAGE_COMPLETED,
                outcome=temporalio.api.update.v1.Outcome(
                    success=temporalio.api.common.v1.Payloads(payloads=[result])
                ),
            )
        }
    )
    client = _client(service, data_converter)

    value = await client.get_workflow_handle("wf").execute_update(
        "update", LARGE, result_type=str
    )

    assert value == "update-result" * 20
    req = service.requests["update_workflow_execution"]
    await _assert_encoded_reference(
        client.data_converter, req.request.input.args.payloads[0]
    )


async def test_query_encodes_args_and_decodes_result_references() -> None:
    data_converter = _data_converter()
    result = await _encoded_reference(data_converter, "query-result" * 20)
    service = _FakeWorkflowService(
        {
            "query_workflow": temporalio.api.workflowservice.v1.QueryWorkflowResponse(
                query_result=temporalio.api.common.v1.Payloads(payloads=[result])
            )
        }
    )
    client = _client(service, data_converter)

    value = await client.get_workflow_handle("wf").query(
        "query", LARGE, result_type=str
    )

    assert value == "query-result" * 20
    await _assert_encoded_reference(
        client.data_converter,
        service.requests["query_workflow"].query.query_args.payloads[0],
    )


async def test_create_schedule_encodes_action_input_reference() -> None:
    service = _FakeWorkflowService(
        {"create_schedule": temporalio.api.workflowservice.v1.CreateScheduleResponse()}
    )
    client = _client(service, _data_converter())

    await client.create_schedule(
        "schedule",
        Schedule(
            action=ScheduleActionStartWorkflow(
                "Workflow", LARGE, id="wf", task_queue="tq"
            ),
            spec=ScheduleSpec(),
        ),
    )

    req = service.requests["create_schedule"]
    await _assert_encoded_reference(
        client.data_converter, req.schedule.action.start_workflow.input.payloads[0]
    )


async def test_async_activity_completion_encodes_references() -> None:
    service = _FakeWorkflowService(
        {
            "respond_activity_task_completed_by_id": temporalio.api.workflowservice.v1.RespondActivityTaskCompletedByIdResponse(),
            "respond_activity_task_failed_by_id": temporalio.api.workflowservice.v1.RespondActivityTaskFailedByIdResponse(),
            "record_activity_task_heartbeat_by_id": temporalio.api.workflowservice.v1.RecordActivityTaskHeartbeatByIdResponse(),
        }
    )
    client = _client(service, _data_converter())
    handle = client.get_async_activity_handle(
        workflow_id="wf", run_id=None, activity_id="activity"
    )

    await handle.heartbeat(LARGE)
    await handle.complete(LARGE)
    await handle.fail(ApplicationError("failed", LARGE))

    await _assert_encoded_reference(
        client.data_converter,
        service.requests["record_activity_task_heartbeat_by_id"].details.payloads[0],
    )
    await _assert_encoded_reference(
        client.data_converter,
        service.requests["respond_activity_task_completed_by_id"].result.payloads[0],
    )
    failure = service.requests["respond_activity_task_failed_by_id"].failure
    await _assert_encoded_reference(
        client.data_converter, failure.application_failure_info.details.payloads[0]
    )


class _LargeHeaderInterceptor(temporalio.client.Interceptor):
    def intercept_client(
        self, next: temporalio.client.OutboundInterceptor
    ) -> temporalio.client.OutboundInterceptor:
        return _LargeHeaderOutbound(next)


class _LargeHeaderOutbound(temporalio.client.OutboundInterceptor):
    async def start_workflow(
        self, input: temporalio.client.StartWorkflowInput
    ) -> temporalio.client.WorkflowHandle[Any, Any]:
        [header] = temporalio.converter.default().payload_converter.to_payloads([LARGE])
        input.headers = {**input.headers, "large": header}
        return await super().start_workflow(input)


async def test_start_workflow_encodes_header_reference_when_headers_use_codec() -> None:
    service = _FakeWorkflowService(
        {
            "start_workflow_execution": temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse(
                run_id="run"
            )
        }
    )
    client = _client(
        service,
        _data_converter(),
        interceptors=[_LargeHeaderInterceptor()],
        header_codec_behavior=HeaderCodecBehavior.CODEC,
    )

    await client.start_workflow("Workflow", id="wf", task_queue="tq")

    req = service.requests["start_workflow_execution"]
    await _assert_encoded_reference(client.data_converter, req.header.fields["large"])


async def test_workflow_result_decodes_reference() -> None:
    data_converter = _data_converter()
    result = await _encoded_reference(data_converter, "workflow-result" * 20)
    service = _FakeWorkflowService(
        {
            "get_workflow_execution_history": _history_response(
                temporalio.api.history.v1.HistoryEvent(
                    event_type=temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
                    workflow_execution_completed_event_attributes=temporalio.api.history.v1.WorkflowExecutionCompletedEventAttributes(
                        result=temporalio.api.common.v1.Payloads(payloads=[result])
                    ),
                )
            )
        }
    )
    client = _client(service, data_converter)

    value = await client.get_workflow_handle("wf", result_type=str).result()

    assert value == "workflow-result" * 20


async def test_workflow_failure_decodes_detail_reference() -> None:
    data_converter = _data_converter()
    failure = temporalio.api.failure.v1.Failure()
    await data_converter.encode_failure(ApplicationError("failed", LARGE), failure)
    await _assert_encoded_reference(
        data_converter, failure.application_failure_info.details.payloads[0]
    )
    service = _FakeWorkflowService(
        {
            "get_workflow_execution_history": _history_response(
                temporalio.api.history.v1.HistoryEvent(
                    event_type=temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED,
                    workflow_execution_failed_event_attributes=temporalio.api.history.v1.WorkflowExecutionFailedEventAttributes(
                        failure=failure
                    ),
                )
            )
        }
    )
    client = _client(service, data_converter)

    with pytest.raises(WorkflowFailureError) as err:
        await client.get_workflow_handle("wf").result()

    assert isinstance(err.value.cause, ApplicationError)
    assert err.value.cause.details == (LARGE,)
