from __future__ import annotations

import contextvars
import logging
import re
import urllib.parse
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Mapping,
    Optional,
    Sequence,
    Union,
)

import nexusrpc.handler
from nexusrpc.handler import CancelOperationContext, StartOperationContext

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.common
from temporalio.nexus.handler._token import WorkflowOperationToken
from temporalio.types import (
    MethodAsyncSingleParam,
    ParamType,
    ReturnType,
    SelfType,
)

if TYPE_CHECKING:
    from temporalio.client import Client, WorkflowHandle


logger = logging.getLogger(__name__)


_current_context: ContextVar[TemporalNexusOperationContext] = ContextVar(
    "temporal-nexus-operation-context"
)


@dataclass
class TemporalNexusOperationContext:
    """
    Context for a Nexus operation being handled by a Temporal Nexus Worker.
    """

    nexus_operation_context: Union[StartOperationContext, CancelOperationContext]

    client: Client
    """The Temporal client in use by the worker handling this Nexus operation."""

    task_queue: str
    """The task queue of the worker handling this Nexus operation."""

    @staticmethod
    def try_current() -> Optional[TemporalNexusOperationContext]:
        return _current_context.get(None)

    @staticmethod
    def current() -> TemporalNexusOperationContext:
        context = TemporalNexusOperationContext.try_current()
        if not context:
            raise RuntimeError("Not in Nexus operation context")
        return context

    @staticmethod
    def set(
        context: TemporalNexusOperationContext,
    ) -> contextvars.Token[TemporalNexusOperationContext]:
        return _current_context.set(context)

    @staticmethod
    def reset(token: contextvars.Token[TemporalNexusOperationContext]) -> None:
        _current_context.reset(token)

    @property
    def temporal_nexus_start_operation_context(
        self,
    ) -> Optional[_TemporalNexusStartOperationContext]:
        ctx = self.nexus_operation_context
        if not isinstance(ctx, StartOperationContext):
            return None
        return _TemporalNexusStartOperationContext(ctx)

    @property
    def temporal_nexus_cancel_operation_context(
        self,
    ) -> Optional[_TemporalNexusCancelOperationContext]:
        ctx = self.nexus_operation_context
        if not isinstance(ctx, CancelOperationContext):
            return None
        return _TemporalNexusCancelOperationContext(ctx)

    # Overload for single-param workflow
    # TODO(nexus-prerelease): support other overloads?
    async def start_workflow(
        self,
        workflow: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
        arg: ParamType,
        *,
        id: str,
        # TODO(nexus-prerelease): Allow client and task queue to be omitted, defaulting to worker's?
        task_queue: str,
        client: Client,
        execution_timeout: Optional[timedelta] = None,
        run_timeout: Optional[timedelta] = None,
        task_timeout: Optional[timedelta] = None,
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy: temporalio.common.WorkflowIDConflictPolicy = temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
        retry_policy: Optional[temporalio.common.RetryPolicy] = None,
        cron_schedule: str = "",
        memo: Optional[Mapping[str, Any]] = None,
        search_attributes: Optional[
            Union[
                temporalio.common.TypedSearchAttributes,
                temporalio.common.SearchAttributes,
            ]
        ] = None,
        static_summary: Optional[str] = None,
        static_details: Optional[str] = None,
        start_delay: Optional[timedelta] = None,
        start_signal: Optional[str] = None,
        start_signal_args: Sequence[Any] = [],
        rpc_metadata: Mapping[str, str] = {},
        rpc_timeout: Optional[timedelta] = None,
        request_eager_start: bool = False,
        priority: temporalio.common.Priority = temporalio.common.Priority.default,
        versioning_override: Optional[temporalio.common.VersioningOverride] = None,
    ) -> WorkflowOperationToken[ReturnType]:
        start_operation_context = self.temporal_nexus_start_operation_context
        if not start_operation_context:
            raise RuntimeError(
                "temporalio.nexus.handler.start_workflow() must be called from within a Nexus start operation context"
            )

        # We must pass nexus_completion_callbacks and workflow_event_links, but these are
        # deliberately not exposed in overloads, hence the type-check violation.
        wf_handle = await client.start_workflow(  # type: ignore
            workflow=workflow,
            arg=arg,
            id=id,
            task_queue=task_queue,
            execution_timeout=execution_timeout,
            run_timeout=run_timeout,
            task_timeout=task_timeout,
            id_reuse_policy=id_reuse_policy,
            id_conflict_policy=id_conflict_policy,
            retry_policy=retry_policy,
            cron_schedule=cron_schedule,
            memo=memo,
            search_attributes=search_attributes,
            static_summary=static_summary,
            static_details=static_details,
            start_delay=start_delay,
            start_signal=start_signal,
            start_signal_args=start_signal_args,
            rpc_metadata=rpc_metadata,
            rpc_timeout=rpc_timeout,
            request_eager_start=request_eager_start,
            priority=priority,
            versioning_override=versioning_override,
            nexus_completion_callbacks=start_operation_context.get_completion_callbacks(),
            workflow_event_links=start_operation_context.get_workflow_event_links(),
            request_id=start_operation_context.nexus_operation_context.request_id,
        )

        start_operation_context.add_outbound_links(wf_handle)

        return WorkflowOperationToken[ReturnType]._unsafe_from_workflow_handle(
            wf_handle
        )


@dataclass
class _TemporalNexusStartOperationContext:
    nexus_operation_context: StartOperationContext

    def get_completion_callbacks(
        self,
    ) -> list[temporalio.common.NexusCompletionCallback]:
        ctx = self.nexus_operation_context
        return (
            [
                # TODO(nexus-prerelease): For WorkflowRunOperation, when it handles the Nexus
                # request, it needs to copy the links to the callback in
                # StartWorkflowRequest.CompletionCallbacks and to StartWorkflowRequest.Links
                # (for backwards compatibility). PR reference in Go SDK:
                # https://github.com/temporalio/sdk-go/pull/1945
                temporalio.common.NexusCompletionCallback(
                    url=ctx.callback_url,
                    header=ctx.callback_headers,
                )
            ]
            if ctx.callback_url
            else []
        )

    def get_workflow_event_links(
        self,
    ) -> list[temporalio.api.common.v1.Link.WorkflowEvent]:
        event_links = []
        for inbound_link in self.nexus_operation_context.inbound_links:
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
            self.nexus_operation_context.outbound_links.append(
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
class _TemporalNexusCancelOperationContext:
    nexus_operation_context: CancelOperationContext


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
        # TODO(nexus-prerelease): RequestIdReference?
    )


def _workflow_event_to_nexus_link(
    workflow_event: temporalio.api.common.v1.Link.WorkflowEvent,
) -> nexusrpc.Link:
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
    return nexusrpc.Link(
        url=urllib.parse.urlunparse((scheme, "", path, "", query_params, "")),
        type=workflow_event.DESCRIPTOR.full_name,
    )


_LINK_URL_PATH_REGEX = re.compile(
    r"^/namespaces/(?P<namespace>[^/]+)/workflows/(?P<workflow_id>[^/]+)/(?P<run_id>[^/]+)/history$"
)


def _nexus_link_to_workflow_event(
    link: nexusrpc.Link,
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
