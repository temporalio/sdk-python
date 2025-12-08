import urllib.parse
from typing import Any

import nexusrpc
import pytest

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.nexus._link_conversion


@pytest.mark.parametrize(
    ["query_param_str", "expected_event_ref"],
    [
        (
            "eventType=NexusOperationScheduled&referenceType=EventReference&eventID=7",
            {
                "event_type": temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                "event_id": 7,
            },
        ),
        # event ID is optional in query params; we set it to 0 in the event ref if missing
        (
            "eventType=NexusOperationScheduled&referenceType=EventReference",
            {
                "event_type": temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                "event_id": 0,
            },
        ),
        # Older server sends EVENT_TYPE_CONSTANT_CASE event type name
        (
            "eventType=EVENT_TYPE_NEXUS_OPERATION_SCHEDULED&referenceType=EventReference",
            {
                "event_type": temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                "event_id": 0,
            },
        ),
    ],
)
def test_query_params_to_event_reference(
    query_param_str: str, expected_event_ref: dict[str, Any]
):
    query_params = urllib.parse.parse_qs(query_param_str)
    event_ref = temporalio.nexus._link_conversion._query_params_to_event_reference(
        query_params
    )
    for k, v in expected_event_ref.items():
        assert getattr(event_ref, k) == v


@pytest.mark.parametrize(
    ["event_ref", "expected_query_param_str"],
    [
        # We always send PascalCase event type names (no EventType prefix)
        (
            {
                "event_type": temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                "event_id": 7,
            },
            "eventType=NexusOperationScheduled&referenceType=EventReference&eventID=7",
        ),
    ],
)
def test_event_reference_to_query_params(
    event_ref: dict[str, Any], expected_query_param_str: str
):
    query_params_str = (
        temporalio.nexus._link_conversion._event_reference_to_query_params(
            temporalio.api.common.v1.Link.WorkflowEvent.EventReference(**event_ref)
        )
    )
    query_params = urllib.parse.parse_qs(query_params_str)
    expected_query_params = urllib.parse.parse_qs(expected_query_param_str)
    assert query_params == expected_query_params


@pytest.mark.parametrize(
    ["query_param_str", "expected_event_ref"],
    [
        (
            "eventType=NexusOperationScheduled&referenceType=RequestIdReference&requestID=req-123",
            {
                "event_type": temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                "request_id": "req-123",
            },
        ),
        # event ID is optional in query params; we leave it unset in the ref if missing
        (
            "eventType=NexusOperationScheduled&referenceType=RequestIdReference",
            {
                "event_type": temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                "request_id": "",
            },
        ),
        # Older server sends EVENT_TYPE_CONSTANT_CASE event type name
        (
            "eventType=EVENT_TYPE_NEXUS_OPERATION_SCHEDULED&referenceType=RequestIdReference&requestID=req-123",
            {
                "event_type": temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                "request_id": "req-123",
            },
        ),
    ],
)
def test_query_params_to_request_id_reference(
    query_param_str: str, expected_event_ref: dict[str, Any]
):
    query_params = urllib.parse.parse_qs(query_param_str)
    event_ref = temporalio.nexus._link_conversion._query_params_to_request_id_reference(
        query_params
    )
    for k, v in expected_event_ref.items():
        assert getattr(event_ref, k) == v


@pytest.mark.parametrize(
    ["event_ref", "expected_query_param_str"],
    [
        # We always send PascalCase event type names (no EventType prefix)
        (
            {
                "event_type": temporalio.api.enums.v1.EventType.EVENT_TYPE_NEXUS_OPERATION_SCHEDULED,
                "request_id": "req-123",
            },
            "eventType=NexusOperationScheduled&referenceType=RequestIdReference&requestID=req-123",
        ),
    ],
)
def test_request_id_reference_to_query_params(
    event_ref: dict[str, Any], expected_query_param_str: str
):
    query_params_str = (
        temporalio.nexus._link_conversion._request_id_reference_to_query_params(
            temporalio.api.common.v1.Link.WorkflowEvent.RequestIdReference(**event_ref)
        )
    )
    query_params = urllib.parse.parse_qs(query_params_str)
    expected_query_params = urllib.parse.parse_qs(expected_query_param_str)
    assert query_params == expected_query_params


@pytest.mark.parametrize(
    ["event", "expected_link"],
    [
        (
            temporalio.api.common.v1.Link.WorkflowEvent(
                namespace="ns",
                workflow_id="wid",
                run_id="rid",
                request_id_ref=temporalio.api.common.v1.Link.WorkflowEvent.RequestIdReference(
                    event_type=temporalio.api.enums.v1.event_type_pb2.EVENT_TYPE_WORKFLOW_TASK_COMPLETED,
                    request_id="req-123",
                ),
            ),
            nexusrpc.Link(
                type=temporalio.api.common.v1.Link.WorkflowEvent.DESCRIPTOR.full_name,
                url="temporal:///namespaces/ns/workflows/wid/rid/history?referenceType=RequestIdReference&requestID=req-123&eventType=WorkflowTaskCompleted",
            ),
        ),
        (
            temporalio.api.common.v1.Link.WorkflowEvent(
                namespace="ns2",
                workflow_id="wid2",
                run_id="rid2",
                event_ref=temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
                    event_id=42,
                    event_type=temporalio.api.enums.v1.event_type_pb2.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED,
                ),
            ),
            nexusrpc.Link(
                type=temporalio.api.common.v1.Link.WorkflowEvent.DESCRIPTOR.full_name,
                url="temporal:///namespaces/ns2/workflows/wid2/rid2/history?eventID=42&eventType=WorkflowExecutionCompleted&referenceType=EventReference",
            ),
        ),
    ],
)
def test_link_conversion_workflow_event_to_link_and_back(
    event: temporalio.api.common.v1.Link.WorkflowEvent, expected_link: nexusrpc.Link
):
    actual_link = temporalio.nexus._link_conversion.workflow_event_to_nexus_link(event)
    assert expected_link == actual_link

    actual_event = temporalio.nexus._link_conversion.nexus_link_to_workflow_event(
        actual_link
    )
    assert event == actual_event


def test_link_conversion_utilities():
    p2c = temporalio.nexus._link_conversion._event_type_pascal_case_to_constant_case
    c2p = temporalio.nexus._link_conversion._event_type_constant_case_to_pascal_case

    for p, c in [
        ("", ""),
        ("A", "A"),
        ("Ab", "AB"),
        ("AbCd", "AB_CD"),
        ("AbCddE", "AB_CDD_E"),
        ("ContainsAOneLetterWord", "CONTAINS_A_ONE_LETTER_WORD"),
        ("NexusOperationScheduled", "NEXUS_OPERATION_SCHEDULED"),
    ]:
        assert p2c(p) == c
        assert c2p(c) == p

    assert p2c("a") == "A"
    assert c2p("A") == "A"
