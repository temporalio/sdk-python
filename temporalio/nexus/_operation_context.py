from __future__ import annotations

import dataclasses
import logging
from collections.abc import (
    Awaitable,
    Callable,
    Generator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    Generic,
    TypeVar,
    overload,
)

from nexusrpc.handler import (
    CancelOperationContext,
    OperationContext,
    StartOperationContext,
)

import temporalio.api.common.v1
import temporalio.api.workflowservice.v1
import temporalio.common
from temporalio.types import (
    MethodAsyncNoParam,
    MethodAsyncSingleParam,
    MultiParamSpec,
    ParamType,
    ReturnType,
    SelfType,
)

from ._link_conversion import (
    nexus_link_to_workflow_event,
    workflow_event_to_nexus_link,
    workflow_execution_started_event_link_from_workflow_handle,
)
from ._token import WorkflowHandle

if TYPE_CHECKING:
    import temporalio.client

# The Temporal Nexus worker always builds a nexusrpc StartOperationContext or
# CancelOperationContext and passes it as the first parameter to the nexusrpc operation
# handler. In addition, it sets one of the following context vars.

_temporal_start_operation_context: ContextVar[_TemporalStartOperationContext] = (
    ContextVar("temporal-start-operation-context")
)

_temporal_cancel_operation_context: ContextVar[_TemporalCancelOperationContext] = (
    ContextVar("temporal-cancel-operation-context")
)

# A Nexus start handler might start zero or more workflows as usual using a Temporal client. In
# addition, it may start one "nexus-backing" workflow, using
# WorkflowRunOperationContext.start_workflow. This context is active while the latter is being done.
# It is thus a narrower context than _temporal_start_operation_context.
_temporal_nexus_backing_workflow_start_context: ContextVar[bool] = ContextVar(
    "temporal-nexus-backing-workflow-start-context"
)


@dataclass(frozen=True)
class Info:
    """Information about the running Nexus operation.

    .. warning::
        This API is experimental and unstable.

    Retrieved inside a Nexus operation handler via :py:func:`info`.
    """

    task_queue: str
    """The task queue of the worker handling this Nexus operation."""


def in_operation() -> bool:
    """Whether the current code is inside a Nexus operation."""
    return _try_temporal_context() is not None


def info() -> Info:
    """Get the current Nexus operation information."""
    return _temporal_context().info()


def client() -> temporalio.client.Client:
    """Get the Temporal client used by the worker handling the current Nexus operation."""
    return _temporal_context().client


def metric_meter() -> temporalio.common.MetricMeter:
    """Get the metric meter for the current Nexus operation."""
    return _temporal_context().metric_meter


def _temporal_context() -> (
    _TemporalStartOperationContext | _TemporalCancelOperationContext
):
    ctx = _try_temporal_context()
    if ctx is None:
        raise RuntimeError("Not in Nexus operation context.")
    return ctx


def _try_temporal_context() -> (
    _TemporalStartOperationContext | _TemporalCancelOperationContext | None
):
    start_ctx = _temporal_start_operation_context.get(None)
    cancel_ctx = _temporal_cancel_operation_context.get(None)
    if start_ctx and cancel_ctx:
        raise RuntimeError("Cannot be in both start and cancel operation contexts.")
    return start_ctx or cancel_ctx


@contextmanager
def _nexus_backing_workflow_start_context() -> Generator[None]:
    token = _temporal_nexus_backing_workflow_start_context.set(True)
    try:
        yield
    finally:
        _temporal_nexus_backing_workflow_start_context.reset(token)


def _in_nexus_backing_workflow_start_context() -> bool:
    return _temporal_nexus_backing_workflow_start_context.get(False)


_OperationCtxT = TypeVar("_OperationCtxT", bound=OperationContext)


@dataclass(kw_only=True)
class _TemporalOperationCtx(Generic[_OperationCtxT]):
    client: temporalio.client.Client
    """The Temporal client in use by the worker handling the current Nexus operation."""

    info: Callable[[], Info]
    """Temporal information about the running Nexus operation."""

    nexus_context: _OperationCtxT
    """Nexus-specific start operation context."""

    _runtime_metric_meter: temporalio.common.MetricMeter
    _metric_meter: temporalio.common.MetricMeter | None = None

    @property
    def metric_meter(self) -> temporalio.common.MetricMeter:
        if not self._metric_meter:
            self._metric_meter = self._runtime_metric_meter.with_additional_attributes(
                {
                    "nexus_service": self.nexus_context.service,
                    "nexus_operation": self.nexus_context.operation,
                    "task_queue": self.info().task_queue,
                }
            )
        return self._metric_meter


@dataclass
class _TemporalStartOperationContext(_TemporalOperationCtx[StartOperationContext]):
    """Context for a Nexus start operation being handled by a Temporal Nexus Worker."""

    @classmethod
    def get(cls) -> _TemporalStartOperationContext:
        ctx = _temporal_start_operation_context.get(None)
        if ctx is None:
            raise RuntimeError("Not in Nexus operation context.")
        return ctx

    def set(self) -> None:
        _temporal_start_operation_context.set(self)

    def _get_callbacks(
        self,
    ) -> list[temporalio.client.Callback]:
        ctx = self.nexus_context
        return (
            [
                NexusCallback(
                    url=ctx.callback_url,
                    headers=ctx.callback_headers,
                )
            ]
            if ctx.callback_url
            else []
        )

    def _get_workflow_event_links(
        self,
    ) -> list[temporalio.api.common.v1.Link.WorkflowEvent]:
        event_links = []
        for inbound_link in self.nexus_context.inbound_links:
            if link := nexus_link_to_workflow_event(inbound_link):
                event_links.append(link)
        return event_links

    def _add_outbound_links(
        self, workflow_handle: temporalio.client.WorkflowHandle[Any, Any]
    ):
        # If links were not sent in StartWorkflowExecutionResponse then construct them.
        wf_event_links: list[temporalio.api.common.v1.Link.WorkflowEvent] = []
        try:
            if isinstance(
                workflow_handle._start_workflow_response,
                temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse,
            ):
                if workflow_handle._start_workflow_response.HasField("link"):
                    if link := workflow_handle._start_workflow_response.link:
                        if link.HasField("workflow_event"):
                            wf_event_links.append(link.workflow_event)
            if not wf_event_links:
                wf_event_links = [
                    workflow_execution_started_event_link_from_workflow_handle(
                        workflow_handle,
                        self.nexus_context.request_id,
                    )
                ]
            self.nexus_context.outbound_links.extend(
                workflow_event_to_nexus_link(link) for link in wf_event_links
            )
        except Exception as e:
            logger.warning(
                f"Failed to create WorkflowExecutionStarted event links for workflow {workflow_handle}: {e}"
            )
        return workflow_handle


class WorkflowRunOperationContext(StartOperationContext):
    """Context received by a workflow run operation.

    .. warning::
        This API is experimental and unstable.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the workflow run operation context."""
        super().__init__(*args, **kwargs)
        self._temporal_context = _TemporalStartOperationContext.get()

    @classmethod
    def _from_start_operation_context(
        cls, ctx: StartOperationContext
    ) -> WorkflowRunOperationContext:
        return cls(
            **{f.name: getattr(ctx, f.name) for f in dataclasses.fields(ctx)},
        )

    @property
    def metric_meter(self) -> temporalio.common.MetricMeter:
        """The metric meter"""
        return self._temporal_context.metric_meter

    # Overload for no-param workflow
    @overload
    async def start_workflow(
        self,
        workflow: MethodAsyncNoParam[SelfType, ReturnType],
        *,
        id: str,
        task_queue: str | None = None,
        execution_timeout: timedelta | None = None,
        run_timeout: timedelta | None = None,
        task_timeout: timedelta | None = None,
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy: temporalio.common.WorkflowIDConflictPolicy = temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
        retry_policy: temporalio.common.RetryPolicy | None = None,
        cron_schedule: str = "",
        memo: Mapping[str, Any] | None = None,
        search_attributes: None
        | (
            temporalio.common.TypedSearchAttributes | temporalio.common.SearchAttributes
        ) = None,
        static_summary: str | None = None,
        static_details: str | None = None,
        start_delay: timedelta | None = None,
        start_signal: str | None = None,
        start_signal_args: Sequence[Any] = [],
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
        request_eager_start: bool = False,
        priority: temporalio.common.Priority = temporalio.common.Priority.default,
        versioning_override: temporalio.common.VersioningOverride | None = None,
    ) -> WorkflowHandle[ReturnType]: ...

    # Overload for single-param workflow
    @overload
    async def start_workflow(
        self,
        workflow: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
        arg: ParamType,
        *,
        id: str,
        task_queue: str | None = None,
        execution_timeout: timedelta | None = None,
        run_timeout: timedelta | None = None,
        task_timeout: timedelta | None = None,
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy: temporalio.common.WorkflowIDConflictPolicy = temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
        retry_policy: temporalio.common.RetryPolicy | None = None,
        cron_schedule: str = "",
        memo: Mapping[str, Any] | None = None,
        search_attributes: None
        | (
            temporalio.common.TypedSearchAttributes | temporalio.common.SearchAttributes
        ) = None,
        static_summary: str | None = None,
        static_details: str | None = None,
        start_delay: timedelta | None = None,
        start_signal: str | None = None,
        start_signal_args: Sequence[Any] = [],
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
        request_eager_start: bool = False,
        priority: temporalio.common.Priority = temporalio.common.Priority.default,
        versioning_override: temporalio.common.VersioningOverride | None = None,
    ) -> WorkflowHandle[ReturnType]: ...

    # Overload for multi-param workflow
    @overload
    async def start_workflow(
        self,
        workflow: Callable[
            Concatenate[SelfType, MultiParamSpec], Awaitable[ReturnType]
        ],
        *,
        args: Sequence[Any],
        id: str,
        task_queue: str | None = None,
        execution_timeout: timedelta | None = None,
        run_timeout: timedelta | None = None,
        task_timeout: timedelta | None = None,
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy: temporalio.common.WorkflowIDConflictPolicy = temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
        retry_policy: temporalio.common.RetryPolicy | None = None,
        cron_schedule: str = "",
        memo: Mapping[str, Any] | None = None,
        search_attributes: None
        | (
            temporalio.common.TypedSearchAttributes | temporalio.common.SearchAttributes
        ) = None,
        static_summary: str | None = None,
        static_details: str | None = None,
        start_delay: timedelta | None = None,
        start_signal: str | None = None,
        start_signal_args: Sequence[Any] = [],
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
        request_eager_start: bool = False,
        priority: temporalio.common.Priority = temporalio.common.Priority.default,
        versioning_override: temporalio.common.VersioningOverride | None = None,
    ) -> WorkflowHandle[ReturnType]: ...

    # Overload for string-name workflow
    @overload
    async def start_workflow(
        self,
        workflow: str,
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
        id: str,
        task_queue: str | None = None,
        result_type: type[ReturnType] | None = None,
        execution_timeout: timedelta | None = None,
        run_timeout: timedelta | None = None,
        task_timeout: timedelta | None = None,
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy: temporalio.common.WorkflowIDConflictPolicy = temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
        retry_policy: temporalio.common.RetryPolicy | None = None,
        cron_schedule: str = "",
        memo: Mapping[str, Any] | None = None,
        search_attributes: None
        | (
            temporalio.common.TypedSearchAttributes | temporalio.common.SearchAttributes
        ) = None,
        static_summary: str | None = None,
        static_details: str | None = None,
        start_delay: timedelta | None = None,
        start_signal: str | None = None,
        start_signal_args: Sequence[Any] = [],
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
        request_eager_start: bool = False,
        priority: temporalio.common.Priority = temporalio.common.Priority.default,
        versioning_override: temporalio.common.VersioningOverride | None = None,
    ) -> WorkflowHandle[ReturnType]: ...

    async def start_workflow(
        self,
        workflow: str | Callable[..., Awaitable[ReturnType]],
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
        id: str,
        task_queue: str | None = None,
        result_type: type | None = None,
        execution_timeout: timedelta | None = None,
        run_timeout: timedelta | None = None,
        task_timeout: timedelta | None = None,
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy: temporalio.common.WorkflowIDConflictPolicy = temporalio.common.WorkflowIDConflictPolicy.UNSPECIFIED,
        retry_policy: temporalio.common.RetryPolicy | None = None,
        cron_schedule: str = "",
        memo: Mapping[str, Any] | None = None,
        search_attributes: None
        | (
            temporalio.common.TypedSearchAttributes | temporalio.common.SearchAttributes
        ) = None,
        static_summary: str | None = None,
        static_details: str | None = None,
        start_delay: timedelta | None = None,
        start_signal: str | None = None,
        start_signal_args: Sequence[Any] = [],
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
        request_eager_start: bool = False,
        priority: temporalio.common.Priority = temporalio.common.Priority.default,
        versioning_override: temporalio.common.VersioningOverride | None = None,
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
        # We must pass nexus_completion_callbacks, workflow_event_links, and request_id,
        # but these are deliberately not exposed in overloads, hence the type-check
        # violation.

        # Here we are starting a "nexus-backing" workflow. That means that the StartWorkflow request
        # contains nexus-specific data such as a completion callback (used by the handler server
        # namespace to deliver the result to the caller namespace when the workflow reaches a
        # terminal state) and inbound links to the caller workflow (attached to history events of
        # the workflow started in the handler namespace, and displayed in the UI).
        with _nexus_backing_workflow_start_context():
            wf_handle = await self._temporal_context.client.start_workflow(  # type: ignore
                workflow=workflow,
                arg=arg,
                args=args,
                id=id,
                task_queue=task_queue or self._temporal_context.info().task_queue,
                result_type=result_type,
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
                callbacks=self._temporal_context._get_callbacks(),
                workflow_event_links=self._temporal_context._get_workflow_event_links(),
                request_id=self._temporal_context.nexus_context.request_id,
            )

        self._temporal_context._add_outbound_links(wf_handle)

        return WorkflowHandle[ReturnType]._unsafe_from_client_workflow_handle(wf_handle)


@dataclass(frozen=True)
class NexusCallback:
    """Nexus callback to attach to events such as workflow completion.

    .. warning::
        This API is experimental and unstable.
    """

    url: str
    """Callback URL."""

    headers: Mapping[str, str]
    """Header to attach to callback request."""


@dataclass
class _TemporalCancelOperationContext(_TemporalOperationCtx[CancelOperationContext]):
    """Context for a Nexus cancel operation being handled by a Temporal Nexus Worker."""

    @classmethod
    def get(cls) -> _TemporalCancelOperationContext:
        ctx = _temporal_cancel_operation_context.get(None)
        if ctx is None:
            raise RuntimeError("Not in Nexus cancel operation context.")
        return ctx

    def set(self) -> None:
        _temporal_cancel_operation_context.set(self)


class LoggerAdapter(logging.LoggerAdapter):
    """Logger adapter that adds Nexus operation context information."""

    def __init__(self, logger: logging.Logger, extra: Mapping[str, Any] | None):
        """Initialize the logger adapter."""
        super().__init__(logger, extra or {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        """Process log records to add Nexus operation context."""
        extra = dict(self.extra or {})
        if tctx := _try_temporal_context():
            extra["service"] = tctx.nexus_context.service
            extra["operation"] = tctx.nexus_context.operation
            extra["task_queue"] = tctx.info().task_queue
        kwargs["extra"] = extra | kwargs.get("extra", {})
        return msg, kwargs


logger = LoggerAdapter(logging.getLogger("temporalio.nexus"), None)
"""Logger that emits additional data describing the current Nexus operation."""
