from __future__ import annotations

import logging
import re
import urllib.parse
from contextvars import ContextVar
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
    Union,
)

import nexusrpc.handler

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.common

if TYPE_CHECKING:
    from temporalio.client import (
        Client,
        WorkflowHandle,
    )

logger = logging.getLogger(__name__)

current_context: ContextVar[Context] = ContextVar("nexus-handler")


@dataclass
class _OperationContext:
    _client: Optional[Client] = None
    _task_queue: Optional[str] = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("Client not set")
        return self._client

    @property
    def task_queue(self) -> str:
        if self._task_queue is None:
            raise RuntimeError("Task queue not set")
        return self._task_queue


# TODO(nexus-prerelease) see https://github.com/temporalio/sdk-python/pull/740/files
@dataclass
class StartOperationContext(_OperationContext, nexusrpc.handler.StartOperationContext):
    def get_completion_callbacks(
        self,
    ) -> list[temporalio.common.NexusCompletionCallback]:
        return (
            [
                # TODO(nexus-prerelease): For WorkflowRunOperation, when it handles the Nexus
                # request, it needs to copy the links to the callback in
                # StartWorkflowRequest.CompletionCallbacks and to StartWorkflowRequest.Links
                # (for backwards compatibility). PR reference in Go SDK:
                # https://github.com/temporalio/sdk-go/pull/1945
                temporalio.common.NexusCompletionCallback(
                    url=self.callback_url, header=self.callback_headers
                )
            ]
            if self.callback_url
            else []
        )

    def get_workflow_event_links(
        self,
    ) -> list[temporalio.api.common.v1.Link.WorkflowEvent]:
        event_links = []
        for inbound_link in self.inbound_links:
            if link := _nexus_link_to_workflow_event(inbound_link):
                event_links.append(link)
        return event_links

    def add_outbound_links(self, workflow_handle: WorkflowHandle[Any, Any]):
        try:
            link = _workflow_event_to_nexus_link(
                _workflow_handle_to_workflow_execution_started_event_link(
                    workflow_handle
                )
            )
        except Exception as e:
            logger.warning(
                f"Failed to create WorkflowExecutionStarted event link for workflow {id}: {e}"
            )
        else:
            self.outbound_links.append(
                # TODO(nexus-prerelease): Before, WorkflowRunOperation was generating an EventReference
                # link to send back to the caller. Now, it checks if the server returned
                # the link in the StartWorkflowExecutionResponse, and if so, send the link
                # from the response to the caller. Fallback to generating the link for
                # backwards compatibility. PR reference in Go SDK:
                # https://github.com/temporalio/sdk-go/pull/1934
                link
            )
        return workflow_handle


@dataclass
class CancelOperationContext(
    _OperationContext, nexusrpc.handler.CancelOperationContext
):
    pass


@dataclass
class Context:
    operation_context: Union[StartOperationContext, CancelOperationContext]

    @property
    def start_operation_context(self) -> Optional[StartOperationContext]:
        if self.operation_context is None:
            raise RuntimeError("Nexus handler operation context not set")
        return (
            self.operation_context
            if isinstance(self.operation_context, StartOperationContext)
            else None
        )

    @property
    def cancel_operation_context(self) -> Optional[CancelOperationContext]:
        if self.operation_context is None:
            raise RuntimeError("Nexus handler operation context not set")
        return (
            self.operation_context
            if isinstance(self.operation_context, CancelOperationContext)
            else None
        )


# TODO(nexus-prerelease): confirm that it is correct not to use event_id in the following functions.
# Should the proto say explicitly that it's optional or how it behaves when it's missing?
def _workflow_handle_to_workflow_execution_started_event_link(
    handle: WorkflowHandle[Any, Any],
) -> temporalio.api.common.v1.Link.WorkflowEvent:
    if handle.first_execution_run_id is None:
        raise ValueError(
            f"Workflow handle {handle} has no first execution run ID. "
            "Cannot create WorkflowExecutionStarted event link."
        )
    return temporalio.api.common.v1.Link.WorkflowEvent(
        namespace=handle._client.namespace,
        workflow_id=handle.id,
        run_id=handle.first_execution_run_id,
        event_ref=temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
            event_type=temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED
        ),
    )


def _workflow_event_to_nexus_link(
    workflow_event: temporalio.api.common.v1.Link.WorkflowEvent,
) -> nexusrpc.handler.Link:
    scheme = "temporal"
    namespace = urllib.parse.quote(workflow_event.namespace)
    workflow_id = urllib.parse.quote(workflow_event.workflow_id)
    run_id = urllib.parse.quote(workflow_event.run_id)
    path = f"/namespaces/{namespace}/workflows/{workflow_id}/{run_id}/history"
    query_params = urllib.parse.urlencode(
        {
            "eventType": temporalio.api.enums.v1.EventType.Name(
                workflow_event.event_ref.event_type
            ),
            "referenceType": "EventReference",
        }
    )
    return nexusrpc.handler.Link(
        url=urllib.parse.urlunparse((scheme, "", path, "", query_params, "")),
        type=workflow_event.DESCRIPTOR.full_name,
    )


_LINK_URL_PATH_REGEX = re.compile(
    r"^/namespaces/(?P<namespace>[^/]+)/workflows/(?P<workflow_id>[^/]+)/(?P<run_id>[^/]+)/history$"
)


def _nexus_link_to_workflow_event(
    link: nexusrpc.handler.Link,
) -> Optional[temporalio.api.common.v1.Link.WorkflowEvent]:
    url = urllib.parse.urlparse(link.url)
    match = _LINK_URL_PATH_REGEX.match(url.path)
    if not match:
        logger.warning(
            f"Invalid Nexus link: {link}. Expected path to match {_LINK_URL_PATH_REGEX.pattern}"
        )
        return None
    try:
        query_params = urllib.parse.parse_qs(url.query)
        [reference_type] = query_params.get("referenceType", [])
        if reference_type != "EventReference":
            raise ValueError(
                f"Expected Nexus link URL query parameter referenceType to be EventReference but got: {reference_type}"
            )
        [event_type_name] = query_params.get("eventType", [])
        event_ref = temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
            event_type=temporalio.api.enums.v1.EventType.Value(event_type_name)
        )
    except ValueError as err:
        logger.warning(
            f"Failed to parse event type from Nexus link URL query parameters: {link} ({err})"
        )
        event_ref = None

    groups = match.groupdict()
    return temporalio.api.common.v1.Link.WorkflowEvent(
        namespace=urllib.parse.unquote(groups["namespace"]),
        workflow_id=urllib.parse.unquote(groups["workflow_id"]),
        run_id=urllib.parse.unquote(groups["run_id"]),
        event_ref=event_ref,
    )
