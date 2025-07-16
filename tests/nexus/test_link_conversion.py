import urllib.parse
from typing import Any

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
    event_ref = temporalio.nexus._link_conversion._query_params_to_event_reference(
        query_param_str
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
