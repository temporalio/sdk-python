from __future__ import annotations

import dataclasses
import logging
import re
import urllib.parse
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    Any,
    Callable,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Union,
)

import nexusrpc.handler
from nexusrpc.handler import CancelOperationContext, StartOperationContext

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.client
import temporalio.common
from temporalio.nexus._token import WorkflowHandle
from temporalio.types import (
    MethodAsyncSingleParam,
    ParamType,
    ReturnType,
    SelfType,
)

# The Temporal Nexus worker always builds a nexusrpc StartOperationContext or
# CancelOperationContext and passes it as the first parameter to the nexusrpc operation
# handler. In addition, it sets one of the following context vars.

_temporal_start_operation_context: ContextVar[TemporalStartOperationContext] = (
    ContextVar("temporal-start-operation-context")
)

_temporal_cancel_operation_context: ContextVar[_TemporalCancelOperationContext] = (
    ContextVar("temporal-cancel-operation-context")
)


@dataclass(frozen=True)
class Info:
    """Information about the running Nexus operation.

    Retrieved inside a Nexus operation handler via :py:func:`info`.
    """

    task_queue: str
    """The task queue of the worker handling this Nexus operation."""


def info() -> Info:
    """
    Get the current Nexus operation information.
    """
    return _temporal_context().info()


def client() -> temporalio.client.Client:
    """
    Get the Temporal client used by the worker handling the current Nexus operation.
    """
    return _temporal_context().client


def _temporal_context() -> (
    Union[TemporalStartOperationContext, _TemporalCancelOperationContext]
):
    ctx = _try_temporal_context()
    if ctx is None:
        raise RuntimeError("Not in Nexus operation context.")
    return ctx


def _try_temporal_context() -> (
    Optional[Union[TemporalStartOperationContext, _TemporalCancelOperationContext]]
):
    start_ctx = _temporal_start_operation_context.get(None)
    cancel_ctx = _temporal_cancel_operation_context.get(None)
    if start_ctx and cancel_ctx:
        raise RuntimeError("Cannot be in both start and cancel operation contexts.")
    return start_ctx or cancel_ctx


@dataclass
class TemporalStartOperationContext:
    """
    Context for a Nexus start operation being handled by a Temporal Nexus Worker.
    """

    nexus_context: StartOperationContext
    """Nexus-specific start operation context."""

    info: Callable[[], Info]
    """Temporal information about the running Nexus operation."""

    client: temporalio.client.Client
    """The Temporal client in use by the worker handling this Nexus operation."""

    @classmethod
    def get(cls) -> TemporalStartOperationContext:
        ctx = _temporal_start_operation_context.get(None)
        if ctx is None:
            raise RuntimeError("Not in Nexus operation context.")
        return ctx

    def set(self) -> None:
        _temporal_start_operation_context.set(self)

    def get_completion_callbacks(
        self,
    ) -> list[temporalio.client.NexusCompletionCallback]:
        ctx = self.nexus_context
        return (
            [
                # TODO(nexus-prerelease): For WorkflowRunOperation, when it handles the Nexus
                # request, it needs to copy the links to the callback in
                # StartWorkflowRequest.CompletionCallbacks and to StartWorkflowRequest.Links
                # (for backwards compatibility). PR reference in Go SDK:
                # https://github.com/temporalio/sdk-go/pull/1945
                temporalio.client.NexusCompletionCallback(
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
        for inbound_link in self.nexus_context.inbound_links:
            if link := _nexus_link_to_workflow_event(inbound_link):
                event_links.append(link)
        return event_links

    def add_outbound_links(
        self, workflow_handle: temporalio.client.WorkflowHandle[Any, Any]
    ):
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
            self.nexus_context.outbound_links.append(
                # TODO(nexus-prerelease): Before, WorkflowRunOperation was generating an EventReference
                # link to send back to the caller. Now, it checks if the server returned
                # the link in the StartWorkflowExecutionResponse, and if so, send the link
                # from the response to the caller. Fallback to generating the link for
                # backwards compatibility. PR reference in Go SDK:
                # https://github.com/temporalio/sdk-go/pull/1934
                link
            )
        return workflow_handle


@dataclass(frozen=True)
class WorkflowRunOperationContext(StartOperationContext):
    _temporal_context: Optional[TemporalStartOperationContext] = None

    @property
    def temporal_context(self) -> TemporalStartOperationContext:
        if not self._temporal_context:
            raise RuntimeError("Temporal context not set")
        return self._temporal_context

    @property
    def nexus_context(self) -> StartOperationContext:
        return self.temporal_context.nexus_context

    @classmethod
    def from_start_operation_context(
        cls, ctx: StartOperationContext
    ) -> WorkflowRunOperationContext:
        return cls(
            _temporal_context=TemporalStartOperationContext.get(),
            **{f.name: getattr(ctx, f.name) for f in dataclasses.fields(ctx)},
        )

    # Overload for single-param workflow
    # TODO(nexus-prerelease): bring over other overloads
    async def start_workflow(
        self,
        workflow: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
        arg: ParamType,
        *,
        id: str,
        task_queue: Optional[str] = None,
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
    ) -> WorkflowHandle[ReturnType]:
        """Start a workflow that will deliver the result of the Nexus operation.

        The workflow will be started in the same namespace as the Nexus worker, using
        the same client as the worker. If task queue is not specified, the worker's task
        queue will be used.

        See :py:meth:`temporalio.client.Client.start_workflow` for all arguments.

        The return value is :py:class:`temporalio.nexus.WorkflowHandle`.

        The workflow will be started as usual, with the following modifications:

        - On workflow completion, Temporal server will deliver the workflow result to
            the Nexus operation caller, using the callback from the Nexus operation start
            request.

        - The request ID from the Nexus operation start request will be used as the
            request ID for the start workflow request.

        - Inbound links to the caller that were submitted in the Nexus start operation
            request will be attached to the started workflow and, outbound links to the
            started workflow will be added to the Nexus start operation response. If the
            Nexus caller is itself a workflow, this means that the workflow in the caller
            namespace web UI will contain links to the started workflow, and vice versa.
        """
        # TODO(nexus-preview): When sdk-python supports on_conflict_options, Typescript does this:
        # if (workflowOptions.workflowIdConflictPolicy === 'USE_EXISTING') {
        #     internalOptions.onConflictOptions = {
        #     attachLinks: true,
        #     attachCompletionCallbacks: true,
        #     attachRequestId: true,
        #     };
        # }

        # We must pass nexus_completion_callbacks, workflow_event_links, and request_id,
        # but these are deliberately not exposed in overloads, hence the type-check
        # violation.
        wf_handle = await self.temporal_context.client.start_workflow(  # type: ignore
            workflow=workflow,
            arg=arg,
            id=id,
            task_queue=task_queue or self.temporal_context.info().task_queue,
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
            nexus_completion_callbacks=self.temporal_context.get_completion_callbacks(),
            workflow_event_links=self.temporal_context.get_workflow_event_links(),
            request_id=self.temporal_context.nexus_context.request_id,
        )

        self.temporal_context.add_outbound_links(wf_handle)

        return WorkflowHandle[ReturnType]._unsafe_from_client_workflow_handle(wf_handle)


@dataclass
class _TemporalCancelOperationContext:
    """
    Context for a Nexus cancel operation being handled by a Temporal Nexus Worker.
    """

    nexus_context: CancelOperationContext
    """Nexus-specific cancel operation context."""

    info: Callable[[], Info]
    """Temporal information about the running Nexus cancel operation."""

    client: temporalio.client.Client
    """The Temporal client in use by the worker handling the current Nexus operation."""

    @classmethod
    def get(cls) -> _TemporalCancelOperationContext:
        ctx = _temporal_cancel_operation_context.get(None)
        if ctx is None:
            raise RuntimeError("Not in Nexus cancel operation context.")
        return ctx

    def set(self) -> None:
        _temporal_cancel_operation_context.set(self)


def _workflow_handle_to_workflow_execution_started_event_link(
    handle: temporalio.client.WorkflowHandle[Any, Any],
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
            # TODO(nexus-prerelease): confirm that it is correct not to use event_id.
            # Should the proto say explicitly that it's optional or how it behaves when it's missing?
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
            # TODO(nexus-prerelease): confirm that it is correct not to use event_id.
            # Should the proto say explicitly that it's optional or how it behaves when it's missing?
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


class _LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]):
        super().__init__(logger, extra or {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        if tctx := _try_temporal_context():
            extra["service"] = tctx.nexus_context.service
            extra["operation"] = tctx.nexus_context.operation
            extra["task_queue"] = tctx.info().task_queue
        kwargs["extra"] = extra | kwargs.get("extra", {})
        return msg, kwargs


logger = _LoggerAdapter(logging.getLogger("temporalio.nexus"), None)
"""Logger that emits additional data describing the current Nexus operation."""
