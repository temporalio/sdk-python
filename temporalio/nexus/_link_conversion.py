from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
)

import nexusrpc

import temporalio.api.common.v1
import temporalio.api.enums.v1

if TYPE_CHECKING:
    import temporalio.client

logger = logging.getLogger(__name__)

_URL_SCHEME = "temporal"


class _LinkType(str, Enum):
    WORKFLOW_EVENT = temporalio.api.common.v1.Link.WorkflowEvent.DESCRIPTOR.full_name
    WORKFLOW = temporalio.api.common.v1.Link.Workflow.DESCRIPTOR.full_name
    NEXUS_OPERATION = temporalio.api.common.v1.Link.NexusOperation.DESCRIPTOR.full_name
    ACTIVITY = temporalio.api.common.v1.Link.Activity.DESCRIPTOR.full_name


@dataclass(frozen=True)
class _LinkPath:
    """The URL path shape for one link type.

    Every link path is /namespaces/{namespace}/{keyword}/{id}/{run_id} with an optional trailing
    segment, so a link type is fully described by which keyword names it, what follows the run ID,
    and which proto field holds the ID.
    """

    keyword: str
    tail: str | None
    id_field: str
    # A standalone Nexus operation need not have a run ID, so its path may carry an empty segment
    # there. The other link types always address a specific run.
    run_id_required: bool = True


_LINK_PATHS: dict[_LinkType, _LinkPath] = {
    _LinkType.WORKFLOW_EVENT: _LinkPath("workflows", "history", "workflow_id"),
    _LinkType.WORKFLOW: _LinkPath("workflows", None, "workflow_id"),
    _LinkType.NEXUS_OPERATION: _LinkPath(
        "nexus-operations", "details", "operation_id", run_id_required=False
    ),
    _LinkType.ACTIVITY: _LinkPath("activities", "details", "activity_id"),
}


def _link_path_regex(path: _LinkPath) -> re.Pattern[str]:
    run_id = "[^/]+" if path.run_id_required else "[^/]*"
    tail = f"/{path.tail}" if path.tail else ""
    return re.compile(
        rf"^/namespaces/(?P<namespace>[^/]+)/{path.keyword}"
        rf"/(?P<id>[^/]+)/(?P<run_id>{run_id}){tail}$"
    )


_LINK_PATH_REGEXES: dict[_LinkType, re.Pattern[str]] = {
    link_type: _link_path_regex(path) for link_type, path in _LINK_PATHS.items()
}


LINK_EVENT_ID_PARAM_NAME = "eventID"
LINK_EVENT_TYPE_PARAM_NAME = "eventType"
LINK_REQUEST_ID_PARAM_NAME = "requestID"
LINK_REFERENCE_TYPE_PARAM_NAME = "referenceType"
LINK_REASON_PARAM_NAME = "reason"

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


def nexus_link_to_temporal_link(
    nexus_link: nexusrpc.Link,
) -> temporalio.api.common.v1.Link | None:
    """Convert a nexusrpc link into a Temporal API Link.

    Returns None when the Nexus link type is invalid or unknown.
    """
    try:
        link_type = _LinkType(nexus_link.type)
    except ValueError:
        logger.warning(f"Invalid Nexus link: unknown link type {nexus_link}")
        return None

    match link_type:
        case _LinkType.WORKFLOW_EVENT:
            return nexus_link_to_workflow_event_link(nexus_link)

        case _LinkType.WORKFLOW:
            return nexus_link_to_workflow_link(nexus_link)

        case _LinkType.NEXUS_OPERATION:
            return nexus_link_to_nexus_operation_link(nexus_link)

        case _LinkType.ACTIVITY:
            return nexus_link_to_activity_link(nexus_link)


def temporal_link_to_nexus_link(
    temporal_link: temporalio.api.common.v1.Link,
) -> nexusrpc.Link | None:
    """Convert a Temporal API Link into a nexusrpc link.

    Returns None when the Temporal link variant is missing.
    """
    match temporal_link.WhichOneof("variant"):
        case "workflow_event":
            return workflow_event_to_nexus_link(temporal_link.workflow_event)

        case "workflow":
            return workflow_to_nexus_link(temporal_link.workflow)

        case "nexus_operation":
            return nexus_operation_to_nexus_link(temporal_link.nexus_operation)

        case "activity":
            return activity_link_to_nexus_link(temporal_link.activity)

        case "batch_job":
            raise NotImplementedError("batch_job links are not supported")

        case None:
            logger.warning("Invalid Temporal link: missing variant")
            return None


def workflow_event_to_nexus_link(
    workflow_event: temporalio.api.common.v1.Link.WorkflowEvent,
) -> nexusrpc.Link:
    """Convert a WorkflowEvent link into a nexusrpc link

    Used when propagating links from a StartWorkflow response to a Nexus start operation
    response.
    """
    query_params = None
    match workflow_event.WhichOneof("reference"):
        case "event_ref":
            query_params = _event_reference_to_query_params(workflow_event.event_ref)
        case "request_id_ref":
            query_params = _request_id_reference_to_query_params(
                workflow_event.request_id_ref
            )
        case _:
            pass

    return nexusrpc.Link(
        url=_build_link_url(
            _LinkType.WORKFLOW_EVENT,
            workflow_event.namespace,
            workflow_event.workflow_id,
            workflow_event.run_id,
            query_params=query_params,
        ),
        type=_LinkType.WORKFLOW_EVENT.value,
    )


def workflow_to_nexus_link(
    workflow: temporalio.api.common.v1.Link.Workflow,
) -> nexusrpc.Link:
    """Convert a Workflow link into a nexusrpc link."""
    query_params = ""
    if workflow.reason:
        query_params = urllib.parse.urlencode(
            {
                LINK_REASON_PARAM_NAME: workflow.reason,
            },
        )

    return nexusrpc.Link(
        url=_build_link_url(
            _LinkType.WORKFLOW,
            workflow.namespace,
            workflow.workflow_id,
            workflow.run_id,
            query_params=query_params,
        ),
        type=_LinkType.WORKFLOW.value,
    )


def nexus_operation_to_nexus_link(
    op_link: temporalio.api.common.v1.Link.NexusOperation,
) -> nexusrpc.Link:
    """Convert a NexusOperation link into a nexusrpc link

    Used when propagating links from a StartNexusOperation response to a Nexus start operation
    response.
    """
    return nexusrpc.Link(
        url=_build_link_url(
            _LinkType.NEXUS_OPERATION,
            op_link.namespace,
            op_link.operation_id,
            op_link.run_id,
        ),
        type=_LinkType.NEXUS_OPERATION.value,
    )


def activity_link_to_nexus_link(
    activity: temporalio.api.common.v1.Link.Activity,
) -> nexusrpc.Link:
    """Convert an Activity link into a nexusrpc link."""
    return nexusrpc.Link(
        url=_build_link_url(
            _LinkType.ACTIVITY,
            activity.namespace,
            activity.activity_id,
            activity.run_id,
        ),
        type=_LinkType.ACTIVITY.value,
    )


def _build_link_url(
    link_type: _LinkType,
    namespace: str,
    id_value: str,
    run_id: str,
    *,
    query_params: str | None = "",
) -> str:
    """Build the URL for a link of the given type."""
    path = _LINK_PATHS[link_type]
    segments = [
        "namespaces",
        urllib.parse.quote(namespace, safe=""),
        path.keyword,
        urllib.parse.quote(id_value, safe=""),
        urllib.parse.quote(run_id, safe=""),
    ]
    if path.tail:
        segments.append(path.tail)
    return _temporal_nexus_url("/" + "/".join(segments), query_params=query_params)


def _temporal_nexus_url(path: str, *, query_params: str | None = "") -> str:
    # urllib will omit '//' from the url if netloc is empty so we add the scheme manually
    return f"temporal://{urllib.parse.urlunparse(('', '', path, '', query_params or '', ''))}"


def _parse_link_url(
    link: nexusrpc.Link, link_type: _LinkType
) -> tuple[str, str, str, dict[str, list[str]]] | None:
    """Return (namespace, id, run_id, query params), or None if the URL is not a link of this type.

    The path is matched exactly, so the workflow and workflow-event shapes -- which differ only by
    the trailing /history -- cannot be mistaken for one another.
    """
    url = urllib.parse.urlparse(link.url)
    if url.scheme != _URL_SCHEME:
        logger.warning(
            f"Invalid Nexus link: {link}. Expected scheme {_URL_SCHEME!r}, got {url.scheme!r}"
        )
        return None
    regex = _LINK_PATH_REGEXES[link_type]
    match = regex.match(url.path)
    if not match:
        logger.warning(
            f"Invalid Nexus link: {link}. Expected path to match {regex.pattern}"
        )
        return None
    return (
        urllib.parse.unquote(match.group("namespace")),
        urllib.parse.unquote(match.group("id")),
        urllib.parse.unquote(match.group("run_id")),
        urllib.parse.parse_qs(url.query),
    )


def _optional_single_query_param(
    query_params: dict[str, list[str]], param_name: str
) -> str:
    match query_params.get(param_name):
        case [param]:
            return param
        case [] | None:
            return ""
        case _:
            raise ValueError(f"Expected {param_name} to have at most 1 value")


def nexus_link_to_workflow_event_link(
    link: nexusrpc.Link,
) -> temporalio.api.common.v1.Link | None:
    """Convert a nexus link into a Temporal WorkflowEvent link

    This is used when propagating links from a Nexus start operation request to a
    StartWorklow request.
    """
    parsed = _parse_link_url(link, _LinkType.WORKFLOW_EVENT)
    if parsed is None:
        return None
    namespace, workflow_id, run_id, query_params = parsed
    try:
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

    workflow_event_link = temporalio.api.common.v1.Link.WorkflowEvent(
        namespace=namespace,
        workflow_id=workflow_id,
        run_id=run_id,
        event_ref=event_ref,
        request_id_ref=request_id_ref,
    )
    return temporalio.api.common.v1.Link(workflow_event=workflow_event_link)


def nexus_link_to_workflow_link(
    link: nexusrpc.Link,
) -> temporalio.api.common.v1.Link | None:
    """Convert a nexus link into a Temporal Workflow link."""
    parsed = _parse_link_url(link, _LinkType.WORKFLOW)
    if parsed is None:
        return None
    namespace, workflow_id, run_id, query_params = parsed
    try:
        reason = _optional_single_query_param(query_params, LINK_REASON_PARAM_NAME)
    except ValueError as err:
        logger.warning(f"Invalid Nexus link: {link}. {err}")
        return None

    workflow_link = temporalio.api.common.v1.Link.Workflow(
        namespace=namespace,
        workflow_id=workflow_id,
        run_id=run_id,
        reason=reason,
    )
    return temporalio.api.common.v1.Link(workflow=workflow_link)


def nexus_link_to_nexus_operation_link(
    nexus_link: nexusrpc.Link,
) -> temporalio.api.common.v1.Link | None:
    """Convert a nexus link into a Temporal NexusOperation link

    This is used when propagating links from a Nexus start operation request to a
    StartNexusOperation request.
    """
    parsed = _parse_link_url(nexus_link, _LinkType.NEXUS_OPERATION)
    if parsed is None:
        return None
    namespace, operation_id, run_id, _ = parsed
    return temporalio.api.common.v1.Link(
        nexus_operation=temporalio.api.common.v1.Link.NexusOperation(
            namespace=namespace,
            operation_id=operation_id,
            run_id=run_id,
        )
    )


def nexus_link_to_activity_link(
    nexus_link: nexusrpc.Link,
) -> temporalio.api.common.v1.Link | None:
    """Convert a Nexus Activity link into a Temporal Activity link."""
    parsed = _parse_link_url(nexus_link, _LinkType.ACTIVITY)
    if parsed is None:
        return None
    namespace, activity_id, run_id, _ = parsed
    return temporalio.api.common.v1.Link(
        activity=temporalio.api.common.v1.Link.Activity(
            namespace=namespace,
            activity_id=activity_id,
            run_id=run_id,
        )
    )


def _event_type_to_param(
    event_type: temporalio.api.enums.v1.EventType.ValueType,
) -> str:
    """Render an event type as the short PascalCase name used on the wire."""
    event_type_name = temporalio.api.enums.v1.EventType.Name(event_type)
    if event_type_name.startswith("EVENT_TYPE_"):
        event_type_name = _event_type_constant_case_to_pascal_case(
            event_type_name.removeprefix("EVENT_TYPE_")
        )
    return event_type_name


def _query_params_to_event_type(
    query_params: dict[str, list[str]],
) -> temporalio.api.enums.v1.EventType.ValueType:
    """Return the event type named in the query params, or raise ValueError.

    Both the prefixed proto name and the short PascalCase name are accepted, since either may
    arrive on the wire.
    """
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
    return temporalio.api.enums.v1.EventType.Value(event_type_name)


def _event_reference_to_query_params(
    event_ref: temporalio.api.common.v1.Link.WorkflowEvent.EventReference,
) -> str:
    params: dict[str, object] = {}
    # An unset event ID is 0, which is not a valid event ID, so omit the param rather than
    # send a zero.
    if event_ref.event_id:
        params[LINK_EVENT_ID_PARAM_NAME] = event_ref.event_id
    params[LINK_EVENT_TYPE_PARAM_NAME] = _event_type_to_param(event_ref.event_type)
    params[LINK_REFERENCE_TYPE_PARAM_NAME] = EVENT_REFERENCE_TYPE
    return urllib.parse.urlencode(params)


def _request_id_reference_to_query_params(
    request_id_ref: temporalio.api.common.v1.Link.WorkflowEvent.RequestIdReference,
) -> str:
    params = {
        LINK_REFERENCE_TYPE_PARAM_NAME: REQUEST_ID_REFERENCE_TYPE,
    }

    if request_id_ref.request_id:
        params[LINK_REQUEST_ID_PARAM_NAME] = request_id_ref.request_id

    params[LINK_EVENT_TYPE_PARAM_NAME] = _event_type_to_param(request_id_ref.event_type)

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

    event_type = _query_params_to_event_type(query_params)

    # event id
    event_id = 0
    [raw_event_id] = query_params.get(LINK_EVENT_ID_PARAM_NAME) or [""]
    if raw_event_id:
        try:
            event_id = int(raw_event_id)
        except ValueError:
            raise ValueError(f"Query params contain invalid event id: {raw_event_id}")

    return temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
        event_type=event_type,
        event_id=event_id,
    )


def _query_params_to_request_id_reference(
    query_params: dict[str, list[str]],
) -> temporalio.api.common.v1.Link.WorkflowEvent.RequestIdReference:
    """Return an EventReference from the query params or raise ValueError."""
    event_type = _query_params_to_event_type(query_params)
    [request_id] = query_params.get(LINK_REQUEST_ID_PARAM_NAME, [""])

    return temporalio.api.common.v1.Link.WorkflowEvent.RequestIdReference(
        request_id=request_id,
        event_type=event_type,
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
