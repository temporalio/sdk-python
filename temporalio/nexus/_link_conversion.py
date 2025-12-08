from __future__ import annotations

import logging
import re
import urllib.parse
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
)

import nexusrpc

import temporalio.api.common.v1
import temporalio.api.enums.v1

if TYPE_CHECKING:
    import temporalio.client

logger = logging.getLogger(__name__)

_LINK_URL_PATH_REGEX = re.compile(
    r"^/namespaces/(?P<namespace>[^/]+)/workflows/(?P<workflow_id>[^/]+)/(?P<run_id>[^/]+)/history$"
)
LINK_EVENT_ID_PARAM_NAME = "eventID"
LINK_EVENT_TYPE_PARAM_NAME = "eventType"
LINK_REQUEST_ID_PARAM_NAME = "requestID"
LINK_REFERENCE_TYPE_PARAM_NAME = "referenceType"

EVENT_REFERENCE_TYPE = "EventReference"
REQUEST_ID_REFERENCE_TYPE = "RequestIdReference"


def workflow_execution_started_event_link_from_workflow_handle(
    handle: temporalio.client.WorkflowHandle[Any, Any], request_id: str
) -> temporalio.api.common.v1.Link.WorkflowEvent:
    """Create a WorkflowEvent link corresponding to a started workflow"""
    if handle.first_execution_run_id is None:
        raise ValueError(
            f"Workflow handle {handle} has no first execution run ID. "
            f"Cannot create WorkflowExecutionStarted event link."
        )

    return temporalio.api.common.v1.Link.WorkflowEvent(
        namespace=handle._client.namespace,
        workflow_id=handle.id,
        run_id=handle.first_execution_run_id,
        request_id_ref=temporalio.api.common.v1.Link.WorkflowEvent.RequestIdReference(
            request_id=request_id,
            event_type=temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED,
        ),
    )


def workflow_event_to_nexus_link(
    workflow_event: temporalio.api.common.v1.Link.WorkflowEvent,
) -> nexusrpc.Link:
    """Convert a WorkflowEvent link into a nexusrpc link

    Used when propagating links from a StartWorkflow response to a Nexus start operation
    response.
    """
    scheme = "temporal"
    namespace = urllib.parse.quote(workflow_event.namespace)
    workflow_id = urllib.parse.quote(workflow_event.workflow_id)
    run_id = urllib.parse.quote(workflow_event.run_id)
    path = f"/namespaces/{namespace}/workflows/{workflow_id}/{run_id}/history"

    query_params = None
    match workflow_event.WhichOneof("reference"):
        case "event_ref":
            query_params = _event_reference_to_query_params(workflow_event.event_ref)
        case "request_id_ref":
            query_params = _request_id_reference_to_query_params(
                workflow_event.request_id_ref
            )

    # urllib will omit '//' from the url if netloc is empty so we add the scheme manually
    url = f"{scheme}://{urllib.parse.urlunparse(('', '', path, '', query_params, ''))}"

    return nexusrpc.Link(
        url=url,
        type=workflow_event.DESCRIPTOR.full_name,
    )


def nexus_link_to_workflow_event(
    link: nexusrpc.Link,
) -> temporalio.api.common.v1.Link.WorkflowEvent | None:
    """Convert a nexus link into a WorkflowEvent link

    This is used when propagating links from a Nexus start operation request to a
    StartWorklow request.
    """
    url = urllib.parse.urlparse(link.url)
    match = _LINK_URL_PATH_REGEX.match(url.path)
    if not match:
        logger.warning(
            f"Invalid Nexus link: {link}. Expected path to match {_LINK_URL_PATH_REGEX.pattern}"
        )
        return None
    try:
        query_params = urllib.parse.parse_qs(url.query)

        request_id_ref = None
        event_ref = None
        match query_params.get(LINK_REFERENCE_TYPE_PARAM_NAME):
            case ["EventReference"]:
                event_ref = _query_params_to_event_reference(query_params)
            case ["RequestIdReference"]:
                request_id_ref = _query_params_to_request_id_reference(query_params)
            case _:
                raise ValueError(
                    f"Invalid Nexus link: {link}. Expected {LINK_REFERENCE_TYPE_PARAM_NAME} to be '{EVENT_REFERENCE_TYPE}' or '{REQUEST_ID_REFERENCE_TYPE}'"
                )

    except ValueError as err:
        logger.warning(
            f"Failed to parse event reference from Nexus link URL query parameters: {link} ({err})"
        )
        return None

    groups = match.groupdict()
    return temporalio.api.common.v1.Link.WorkflowEvent(
        namespace=urllib.parse.unquote(groups["namespace"]),
        workflow_id=urllib.parse.unquote(groups["workflow_id"]),
        run_id=urllib.parse.unquote(groups["run_id"]),
        event_ref=event_ref,
        request_id_ref=request_id_ref,
    )


def _event_reference_to_query_params(
    event_ref: temporalio.api.common.v1.Link.WorkflowEvent.EventReference,
) -> str:
    event_type_name = temporalio.api.enums.v1.EventType.Name(event_ref.event_type)
    if event_type_name.startswith("EVENT_TYPE_"):
        event_type_name = _event_type_constant_case_to_pascal_case(
            event_type_name.removeprefix("EVENT_TYPE_")
        )
    return urllib.parse.urlencode(
        {
            LINK_EVENT_ID_PARAM_NAME: event_ref.event_id,
            LINK_EVENT_TYPE_PARAM_NAME: event_type_name,
            LINK_REFERENCE_TYPE_PARAM_NAME: EVENT_REFERENCE_TYPE,
        }
    )


def _request_id_reference_to_query_params(
    request_id_ref: temporalio.api.common.v1.Link.WorkflowEvent.RequestIdReference,
) -> str:
    params = {
        LINK_REFERENCE_TYPE_PARAM_NAME: REQUEST_ID_REFERENCE_TYPE,
    }

    if request_id_ref.request_id:
        params[LINK_REQUEST_ID_PARAM_NAME] = request_id_ref.request_id

    event_type_name = temporalio.api.enums.v1.EventType.Name(request_id_ref.event_type)
    if event_type_name.startswith("EVENT_TYPE_"):
        event_type_name = _event_type_constant_case_to_pascal_case(
            event_type_name.removeprefix("EVENT_TYPE_")
        )
    params[LINK_EVENT_TYPE_PARAM_NAME] = event_type_name

    return urllib.parse.urlencode(params)


def _query_params_to_event_reference(
    query_params: dict[str, list[str]],
) -> temporalio.api.common.v1.Link.WorkflowEvent.EventReference:
    """Return an EventReference from the query params or raise ValueError."""
    [reference_type] = query_params.get(LINK_REFERENCE_TYPE_PARAM_NAME) or [""]
    if reference_type != EVENT_REFERENCE_TYPE:
        raise ValueError(
            f"Expected Nexus link URL query parameter referenceType to be EventReference but got: {reference_type}"
        )

    # event type
    match query_params.get(LINK_EVENT_TYPE_PARAM_NAME):
        case None:
            raise ValueError(f"query params do not contain event type: {query_params}")

        case [raw_event_type_name] if raw_event_type_name.startswith("EVENT_TYPE_"):
            event_type_name = raw_event_type_name

        case [raw_event_type_name] if re.match("[A-Z][a-z]", raw_event_type_name):
            event_type_name = "EVENT_TYPE_" + _event_type_pascal_case_to_constant_case(
                raw_event_type_name
            )

        case raw_event_type_name:
            raise ValueError(f"Invalid event type name: {raw_event_type_name}")

    # event id
    event_id = 0
    [raw_event_id] = query_params.get(LINK_EVENT_ID_PARAM_NAME) or [""]
    if raw_event_id:
        try:
            event_id = int(raw_event_id)
        except ValueError:
            raise ValueError(f"Query params contain invalid event id: {raw_event_id}")

    return temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
        event_type=temporalio.api.enums.v1.EventType.Value(event_type_name),
        event_id=event_id,
    )


def _query_params_to_request_id_reference(
    query_params: dict[str, list[str]],
) -> temporalio.api.common.v1.Link.WorkflowEvent.RequestIdReference:
    """Return an EventReference from the query params or raise ValueError."""
    # event type
    match query_params.get(LINK_EVENT_TYPE_PARAM_NAME):
        case None:
            raise ValueError(f"query params do not contain event type: {query_params}")

        case [raw_event_type_name] if raw_event_type_name.startswith("EVENT_TYPE_"):
            event_type_name = raw_event_type_name

        case [raw_event_type_name] if re.match("[A-Z][a-z]", raw_event_type_name):
            event_type_name = "EVENT_TYPE_" + _event_type_pascal_case_to_constant_case(
                raw_event_type_name
            )

        case raw_event_type_name:
            raise ValueError(f"Invalid event type name: {raw_event_type_name}")

    [request_id] = query_params.get(LINK_REQUEST_ID_PARAM_NAME, [""])

    return temporalio.api.common.v1.Link.WorkflowEvent.RequestIdReference(
        request_id=request_id,
        event_type=temporalio.api.enums.v1.EventType.Value(event_type_name),
    )


def _event_type_constant_case_to_pascal_case(s: str) -> str:
    """Convert a CONSTANT_CASE string to PascalCase.

    >>> _event_type_constant_case_to_pascal_case("NEXUS_OPERATION_SCHEDULED")
    "NexusOperationScheduled"
    """
    return re.sub(r"(\b|_)([a-z])", lambda m: m.groups()[1].upper(), s.lower())


def _event_type_pascal_case_to_constant_case(s: str) -> str:
    """Convert a PascalCase string to CONSTANT_CASE.

    >>> _event_type_pascal_case_to_constant_case("NexusOperationScheduled")
    "NEXUS_OPERATION_SCHEDULED"
    """
    return re.sub(r"([A-Z])", r"_\1", s).lstrip("_").upper()
