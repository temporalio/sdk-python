"""Client for accessing Temporal."""

import logging
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum
from typing import Any, Generic, Iterable, Mapping, Optional, TypeVar, Union, cast

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.failure.v1
import temporalio.api.history.v1
import temporalio.api.taskqueue.v1
import temporalio.api.workflowservice.v1
import temporalio.common
import temporalio.converter
import temporalio.failure
import temporalio.workflow_service

logger = logging.getLogger(__name__)


class WorkflowIDReusePolicy(IntEnum):
    """How already-in-use workflow IDs are handled on start.

    See :py:class:`temporalio.api.enums.v1.WorkflowIdReusePolicy`.
    """

    ALLOW_DUPLICATE = int(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE`."""

    ALLOW_DUPLICATE_FAILED_ONLY = int(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE_FAILED_ONLY
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_ALLOW_DUPLICATE_FAILED_ONLY`."""

    REJECT_DUPLICATE = int(
        temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE`."""


class WorkflowQueryRejectCondition(IntEnum):
    """Whether a query should be rejected in certain conditions.

    See :py:class:`temporalio.api.enums.v1.QueryRejectCondition`.
    """

    NONE = int(temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NONE)
    """See :py:attr:`temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NONE`."""

    NOT_OPEN = int(
        temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NOT_OPEN
    )
    """See :py:attr:`temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NOT_OPEN`."""

    NOT_COMPLETED_CLEANLY = int(
        temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NOT_COMPLETED_CLEANLY
    )
    """See :py:attr:`temporalio.api.enums.v1.QueryRejectCondition.QUERY_REJECT_CONDITION_NOT_COMPLETED_CLEANLY`."""


class WorkflowExecutionStatus(IntEnum):
    """Status of a workflow execution.

    See :py:class:`temporalio.api.enums.v1.WorkflowExecutionStatus`.
    """

    RUNNING = int(
        temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_RUNNING
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_RUNNING`."""

    COMPLETED = int(
        temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_COMPLETED
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_COMPLETED`."""

    FAILED = int(
        temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_FAILED
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_FAILED`."""

    CANCELED = int(
        temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_CANCELED
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_CANCELED`."""

    TERMINATED = int(
        temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_TERMINATED
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_TERMINATED`."""

    CONTINUED_AS_NEW = int(
        temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_CONTINUED_AS_NEW
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_CONTINUED_AS_NEW`."""

    TIMED_OUT = int(
        temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_TIMED_OUT
    )
    """See :py:attr:`temporalio.api.enums.v1.WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_TIMED_OUT`."""


class Client:
    """Client for accessing Temporal.

    Most users will use :py:meth:`connect` to create a client. The
    :py:attr:`service` property provides access to a raw gRPC client. To create
    another client, like for a different namespace, :py:func:`Client` may be
    directly instantiated with a :py:attr:`service` of another.
    """

    @staticmethod
    async def connect(
        target_url: str,
        *,
        namespace: str = "default",
        identity: str = f"{os.getpid()}@{socket.gethostname()}",
        data_converter: temporalio.converter.DataConverter = temporalio.converter.default(),
        interceptors: Iterable["Interceptor"] = [],
        workflow_query_reject_condition: Optional[WorkflowQueryRejectCondition] = None,
    ) -> "Client":
        """Connect to a Temporal server.

        Args:
            target_url: URL for the Temporal server. For local development, this
                is often "http://localhost:7233".
            namespace: Namespace to use for client calls.
            identity: Identity to use for client calls.
            data_converter: Data converter to use for all data conversions
                to/from payloads.
            interceptors: Set of interceptors that are chained together to allow
                intercepting of client calls. The earlier interceptors wrap the
                later ones.
            workflow_query_reject_condition: When to reject a query.
        """
        return Client(
            await temporalio.workflow_service.WorkflowService.connect(target_url),
            namespace=namespace,
            identity=identity,
            data_converter=data_converter,
            interceptors=interceptors,
            workflow_query_reject_condition=workflow_query_reject_condition,
        )

    def __init__(
        self,
        service: temporalio.workflow_service.WorkflowService,
        *,
        namespace: str = "default",
        identity: str = f"{os.getpid()}@{socket.gethostname()}",
        data_converter: temporalio.converter.DataConverter = temporalio.converter.default(),
        interceptors: Iterable["Interceptor"] = [],
        workflow_query_reject_condition: Optional[WorkflowQueryRejectCondition] = None,
    ):
        """Create a Temporal client from a workflow service.

        See :py:meth:`connect` for details on the parameters.
        """
        self._service = service
        self._namespace = namespace
        self._identity = identity
        self._data_converter = data_converter
        self._interceptors = interceptors
        self._workflow_query_reject_condition = workflow_query_reject_condition

        # Iterate over interceptors in reverse building the impl
        self._impl: OutboundInterceptor = _ClientImpl(self)
        for interceptor in reversed(list(interceptors)):
            self._impl = interceptor.intercept_client(self._impl)

    @property
    def service(self) -> temporalio.workflow_service.WorkflowService:
        """Raw gRPC service for this client."""
        return self._service

    @property
    def namespace(self) -> str:
        """Namespace used in calls by this client."""
        return self._namespace

    @property
    def identity(self) -> str:
        """Identity used in calls by this client."""
        return self._identity

    @property
    def data_converter(self) -> temporalio.converter.DataConverter:
        """Data converter used by this client."""
        return self._data_converter

    async def start_workflow(
        self,
        workflow: str,
        *args: Any,
        id: str,
        task_queue: str,
        execution_timeout: Optional[timedelta] = None,
        run_timeout: Optional[timedelta] = None,
        task_timeout: Optional[timedelta] = None,
        id_reuse_policy: WorkflowIDReusePolicy = WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        retry_policy: Optional[temporalio.common.RetryPolicy] = None,
        cron_schedule: str = "",
        memo: Optional[Mapping[str, Any]] = None,
        search_attributes: Optional[Mapping[str, Any]] = None,
        header: Optional[Mapping[str, Any]] = None,
        start_signal: Optional[str] = None,
        start_signal_args: Iterable[Any] = [],
    ) -> "WorkflowHandle[Any]":
        """Start a workflow and return its handle.

        Args:
            workflow: Name of the workflow to start.
            args: Arguments for the workflow if any.
            id: Unique identifier for the workflow execution.
            task_queue: Task queue to run the workflow on.
            execution_timeout: Total workflow execution timeout including
                retries and continue as new.
            run_timeout: Timeout of a single workflow run.
            task_timeout: Timeout of a single workflow task.
            id_reuse_policy: How already-existing IDs are treated.
            retry_policy: Retry policy for the workflow.
            cron_schedule: See https://docs.temporal.io/docs/content/what-is-a-temporal-cron-job/
            memo: Memo for the workflow.
            search_attributes: Search attributes for the workflow.
            header: Header for the workflow.
            start_signal: If present, this signal is sent as signal-with-start
                instead of traditional workflow start.
            start_signal_args: Arguments for start_signal if start_signal
                present.

        Returns:
            A workflow handle to the started/existing workflow.
            :py:attr:`WorkflowHandle.run_id` will be populated with the current
            run ID.
        """
        return await self._impl.start_workflow(
            StartWorkflowInput(
                workflow=workflow,
                args=args,
                id=id,
                task_queue=task_queue,
                execution_timeout=execution_timeout,
                run_timeout=run_timeout,
                task_timeout=task_timeout,
                id_reuse_policy=id_reuse_policy,
                retry_policy=retry_policy,
                cron_schedule=cron_schedule,
                memo=memo,
                search_attributes=search_attributes,
                header=header,
                start_signal=start_signal,
                start_signal_args=start_signal_args,
            )
        )

    async def execute_workflow(
        self,
        workflow: str,
        *args: Any,
        id: str,
        task_queue: str,
        execution_timeout: Optional[timedelta] = None,
        run_timeout: Optional[timedelta] = None,
        task_timeout: Optional[timedelta] = None,
        id_reuse_policy: WorkflowIDReusePolicy = WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        retry_policy: Optional[temporalio.common.RetryPolicy] = None,
        cron_schedule: str = "",
        memo: Optional[Mapping[str, Any]] = None,
        search_attributes: Optional[Mapping[str, Any]] = None,
        header: Optional[Mapping[str, Any]] = None,
        start_signal: Optional[str] = None,
        start_signal_args: Iterable[Any] = [],
    ) -> Any:
        """Start a workflow and wait for completion.

        This is a shortcut for :py:meth:`start_workflow` +
        :py:meth:`WorkflowHandle.result`.
        """
        return await (
            await self.start_workflow(
                workflow,
                *args,
                task_queue=task_queue,
                id=id,
                execution_timeout=execution_timeout,
                run_timeout=run_timeout,
                task_timeout=task_timeout,
                id_reuse_policy=id_reuse_policy,
                retry_policy=retry_policy,
                cron_schedule=cron_schedule,
                memo=memo,
                search_attributes=search_attributes,
                header=header,
                start_signal=start_signal,
                start_signal_args=start_signal_args,
            )
        ).result()

    def get_workflow_handle(
        self, workflow_id: str, *, run_id: Optional[str] = None
    ) -> "WorkflowHandle[Any]":
        """Get a workflow handle to an existing workflow by its ID.

        Args:
            workflow_id: Workflow ID to get a handle to.
            run_id: Run ID that will be used for all calls.
        """
        return WorkflowHandle(self, workflow_id, run_id=run_id)


T = TypeVar("T")


class WorkflowHandle(Generic[T]):
    """Handle for interacting with a workflow.

    This is usually created via :py:meth:`Client.get_workflow_handle` or
    returned from :py:meth:`Client.start_workflow`/:py:meth:`Client.execute_workflow`.
    """

    SELF_RUN_ID = "_<self_run_id>_"

    def __init__(
        self, client: Client, id: str, *, run_id: Optional[str] = None
    ) -> None:
        """Create workflow handle."""
        self._client = client
        self._id = id
        self._run_id = run_id

    @property
    def id(self) -> str:
        """ID for the workflow."""
        return self._id

    @property
    def run_id(self) -> Optional[str]:
        """Run ID used for calls on this handle if present."""
        return self._run_id

    async def result(
        self, *, starting_run_id: Optional[str] = SELF_RUN_ID, follow_runs: bool = True
    ) -> T:
        """Wait for result of the workflow.

        Args:
            starting_run_id: Run ID to fetch result for. Defaults to using
                :py:meth:`run_id`. If set to None or there is no
                :py:meth:`run_id`, this will get the latest result for the
                workflow ID.
            follow_runs: If true (default), workflow runs will be continually
                fetched, until the most recent one is found. If false, the first
                result is used.

        Returns:
            Result of the workflow after being converted by the data converter.

        Raises:
            Exception: Any failure of the workflow.
        """
        if starting_run_id == WorkflowHandle.SELF_RUN_ID:
            starting_run_id = self._run_id
        req = temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryRequest(
            namespace=self._client.namespace,
            execution=temporalio.api.common.v1.WorkflowExecution(
                workflow_id=self._id, run_id=starting_run_id or ""
            ),
            wait_new_event=True,
            history_event_filter_type=temporalio.api.enums.v1.HistoryEventFilterType.HISTORY_EVENT_FILTER_TYPE_CLOSE_EVENT,
            skip_archival=True,
        )
        while True:
            resp = await self._client.service.get_workflow_execution_history(req)
            # Continually ask for pages until we get close
            if len(resp.history.events) == 0:
                req.next_page_token = resp.next_page_token
                continue
            elif len(resp.history.events) != 1:
                raise RuntimeError(
                    f"Expected single close event, got {len(resp.history.events)}"
                )
            event = resp.history.events[0]
            if event.HasField("workflow_execution_completed_event_attributes"):
                complete_attr = event.workflow_execution_completed_event_attributes
                # Follow execution
                if follow_runs and complete_attr.new_execution_run_id:
                    req.execution.run_id = complete_attr.new_execution_run_id
                    req.next_page_token = b""
                    continue
                # Ignoring anything after the first response like TypeScript
                if not complete_attr.result:
                    return cast(T, None)
                results = await self._client.data_converter.decode(
                    complete_attr.result.payloads
                )
                if not results:
                    return cast(T, None)
                elif len(results) > 1:
                    logger.warning("Expected single result, got %s", len(results))
                return cast(T, results[0])
            elif event.HasField("workflow_execution_failed_event_attributes"):
                fail_attr = event.workflow_execution_failed_event_attributes
                # Follow execution
                if follow_runs and fail_attr.new_execution_run_id:
                    req.execution.run_id = fail_attr.new_execution_run_id
                    req.next_page_token = b""
                    continue
                raise WorkflowFailureError(
                    cause=await temporalio.failure.FailureError.from_proto(
                        fail_attr.failure, self._client.data_converter
                    )
                )
            elif event.HasField("workflow_execution_canceled_event_attributes"):
                cancel_attr = event.workflow_execution_canceled_event_attributes
                details = []
                if cancel_attr.details and cancel_attr.details.payloads:
                    details = await self._client.data_converter.decode(
                        cancel_attr.details.payloads
                    )
                raise WorkflowFailureError(
                    cause=temporalio.failure.FailureError(
                        "Workflow cancelled",
                        temporalio.failure.CancelledFailure(*details),
                    )
                )
            elif event.HasField("workflow_execution_terminated_event_attributes"):
                term_attr = event.workflow_execution_terminated_event_attributes
                details = []
                if term_attr.details and term_attr.details.payloads:
                    details = await self._client.data_converter.decode(
                        term_attr.details.payloads
                    )
                raise WorkflowFailureError(
                    cause=temporalio.failure.FailureError(
                        term_attr.reason or "Workflow terminated",
                        temporalio.failure.TerminatedFailure(
                            *details,
                            reason=term_attr.reason or None,
                        ),
                    )
                )
            elif event.HasField("workflow_execution_timed_out_event_attributes"):
                time_attr = event.workflow_execution_timed_out_event_attributes
                # Follow execution
                if follow_runs and time_attr.new_execution_run_id:
                    req.execution.run_id = time_attr.new_execution_run_id
                    req.next_page_token = b""
                    continue
                raise WorkflowFailureError(
                    cause=temporalio.failure.FailureError(
                        "Workflow timed out",
                        temporalio.failure.TimeoutFailure(
                            temporalio.failure.TimeoutType.START_TO_CLOSE
                        ),
                    )
                )
            elif event.HasField("workflow_execution_continued_as_new_event_attributes"):
                cont_attr = event.workflow_execution_continued_as_new_event_attributes
                if not cont_attr.new_execution_run_id:
                    raise RuntimeError(
                        "Unexpectedly missing new run ID from continue as new"
                    )
                # Follow execution
                if follow_runs:
                    req.execution.run_id = cont_attr.new_execution_run_id
                    req.next_page_token = b""
                    continue
                raise WorkflowContinuedAsNewError(cont_attr.new_execution_run_id)
            else:
                raise RuntimeError(
                    f"Unexpected close event attribute of {event.WhichOneof('attributes')}"
                )

    async def cancel(
        self,
        *,
        run_id: Optional[str] = SELF_RUN_ID,
        first_execution_run_id: Optional[None],
    ) -> None:
        """Cancel the workflow.

        Args:
            run_id: Run ID to cancel. Defaults to using :py:meth:`run_id`. If
                set to None or there is no :py:meth:`run_id`, this will cancel
                the latest run for the workflow ID.
            first_execution_run_id: First run ID that started the workflow. If
                set, the cancellation makes sure that the workflow was started
                with the given run ID.

        TODO(cretz): Raises
        """
        if run_id == WorkflowHandle.SELF_RUN_ID:
            run_id = self._run_id
        await self._client._impl.cancel_workflow(
            CancelWorkflowInput(
                id=self._id,
                run_id=run_id,
                first_execution_run_id=first_execution_run_id,
            )
        )

    # TODO(cretz): Wrap the result in Python-friendlier type?
    async def describe(
        self,
        *,
        run_id: Optional[str] = SELF_RUN_ID,
    ) -> temporalio.api.workflowservice.v1.DescribeWorkflowExecutionResponse:
        """Get workflow details.

        Args:
            run_id: Run ID to describe. Defaults to using :py:meth:`run_id`. If
                set to None or there is no :py:meth:`run_id`, this will describe
                the latest run for the workflow ID.

        Returns:
            Workflow details.

        TODO(cretz): Raises
        """
        if run_id == WorkflowHandle.SELF_RUN_ID:
            run_id = self._run_id
        return await self._client.service.describe_workflow_execution(
            temporalio.api.workflowservice.v1.DescribeWorkflowExecutionRequest(
                namespace=self._client.namespace,
                execution=temporalio.api.common.v1.WorkflowExecution(
                    workflow_id=self._id,
                    run_id=run_id or "",
                ),
            )
        )

    async def query(
        self, name: str, *args: Any, run_id: Optional[str] = SELF_RUN_ID
    ) -> Any:
        """Query the workflow.

        Args:
            name: Query name on the workflow.
            args: Query arguments.
            run_id: Run ID to query. Defaults to using :py:meth:`run_id`. If set
                to None or there is no :py:meth:`run_id`, this will query the
                latest run for the workflow ID.

        Returns:
            Result of the query.

        TODO(cretz): Raises
        """
        if run_id == WorkflowHandle.SELF_RUN_ID:
            run_id = self._run_id
        return await self._client._impl.query_workflow(
            QueryWorkflowInput(
                id=self._id,
                run_id=run_id,
                query=name,
                args=args,
                reject_condition=self._client._workflow_query_reject_condition,
            )
        )

    async def signal(
        self, name: str, *args: Any, run_id: Optional[str] = SELF_RUN_ID
    ) -> None:
        """Send a signal to the workflow.

        Args:
            name: Signal name on the workflow.
            args: Signal arguments.
            run_id: Run ID to signal. Defaults to using :py:meth:`run_id`. If
                set to None or there is no :py:meth:`run_id`, this will query
                the latest run for the workflow ID.

        TODO(cretz): Raises
        """
        if run_id == WorkflowHandle.SELF_RUN_ID:
            run_id = self._run_id
        await self._client._impl.signal_workflow(
            SignalWorkflowInput(
                id=self._id,
                run_id=run_id,
                signal=name,
                args=args,
            )
        )

    async def terminate(
        self,
        *args: Any,
        reason: Optional[str] = None,
        run_id: Optional[str] = SELF_RUN_ID,
        first_execution_run_id: Optional[None],
    ) -> None:
        """Terminate the workflow.

        Args:
            args: Details to store on the termination.
            reason: Reason for the termination.
            run_id: Run ID to terminate. Defaults to using :py:meth:`run_id`. If
                set to None or there is no :py:meth:`run_id`, this will
                terminate the latest run for the workflow ID.
            first_execution_run_id: First run ID that started the workflow. If
                set, the termination makes sure that the workflow was started
                with the given run ID.

        TODO(cretz): Raises
        """
        if run_id == WorkflowHandle.SELF_RUN_ID:
            run_id = self._run_id
        await self._client._impl.terminate_workflow(
            TerminateWorkflowInput(
                id=self._id,
                run_id=run_id,
                args=args,
                reason=reason,
                first_execution_run_id=first_execution_run_id,
            )
        )


@dataclass
class StartWorkflowInput:
    """Input for :py:meth:`OutboundInterceptor.start_workflow`."""

    workflow: str
    args: Iterable[Any]
    id: str
    task_queue: str
    execution_timeout: Optional[timedelta]
    run_timeout: Optional[timedelta]
    task_timeout: Optional[timedelta]
    id_reuse_policy: WorkflowIDReusePolicy
    retry_policy: Optional[temporalio.common.RetryPolicy]
    cron_schedule: str
    memo: Optional[Mapping[str, Any]]
    search_attributes: Optional[Mapping[str, Any]]
    header: Optional[Mapping[str, Any]]
    start_signal: Optional[str]
    start_signal_args: Iterable[Any]


@dataclass
class CancelWorkflowInput:
    """Input for :py:meth:`OutboundInterceptor.cancel_workflow`."""

    id: str
    run_id: Optional[str]
    first_execution_run_id: Optional[str]


@dataclass
class QueryWorkflowInput:
    """Input for :py:meth:`OutboundInterceptor.query_workflow`."""

    id: str
    run_id: Optional[str]
    query: str
    args: Iterable[Any]
    reject_condition: Optional[WorkflowQueryRejectCondition]


@dataclass
class SignalWorkflowInput:
    """Input for :py:meth:`OutboundInterceptor.signal_workflow`."""

    id: str
    run_id: Optional[str]
    signal: str
    args: Iterable[Any]


@dataclass
class TerminateWorkflowInput:
    """Input for :py:meth:`OutboundInterceptor.terminate_workflow`."""

    id: str
    run_id: Optional[str]
    first_execution_run_id: Optional[str]
    args: Iterable[Any]
    reason: Optional[str]


class Interceptor:
    """Interceptor for clients.

    This should be extended by any client interceptors.
    """

    def intercept_client(self, next: "OutboundInterceptor") -> "OutboundInterceptor":
        """Method called for intercepting a client.

        Args:
            next: The underlying outbound interceptor this interceptor should
                delegate to.

        Returns:
            The new interceptor that will be called for each client call.
        """
        return next


class OutboundInterceptor:
    """OutboundInterceptor for intercepting client calls.

    This should be extended by any client outbound interceptors.
    """

    def __init__(self, next: "OutboundInterceptor") -> None:
        """Create the outbound interceptor.

        Args:
            next: The next interceptor in the chain. The default implementation
                of all calls is to delegate to the next interceptor.
        """
        self.next = next

    async def start_workflow(self, input: StartWorkflowInput) -> WorkflowHandle[Any]:
        """Called for every :py:meth:`Client.start_workflow` call."""
        return await self.next.start_workflow(input)

    async def cancel_workflow(self, input: CancelWorkflowInput) -> None:
        """Called for every :py:meth:`WorkflowHandle.cancel` call."""
        await self.next.cancel_workflow(input)

    async def query_workflow(self, input: QueryWorkflowInput) -> Any:
        """Called for every :py:meth:`WorkflowHandle.query` call."""
        return await self.next.query_workflow(input)

    async def signal_workflow(self, input: SignalWorkflowInput) -> None:
        """Called for every :py:meth:`WorkflowHandle.signal` call."""
        await self.next.signal_workflow(input)

    async def terminate_workflow(self, input: TerminateWorkflowInput) -> None:
        """Called for every :py:meth:`WorkflowHandle.terminate` call."""
        await self.next.terminate_workflow(input)


class _ClientImpl(OutboundInterceptor):
    def __init__(self, client: Client) -> None:
        # We are intentionally not calling the base class's __init__ here
        self._client = client

    async def start_workflow(self, input: StartWorkflowInput) -> WorkflowHandle[Any]:
        # Build request
        req: Union[
            temporalio.api.workflowservice.v1.StartWorkflowExecutionRequest,
            temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest,
        ]
        if input.start_signal is not None:
            req = temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest(
                signal_name=input.start_signal
            )
            if input.start_signal_args:
                req.signal_input.payloads.extend(
                    await self._client.data_converter.encode(input.start_signal_args)
                )
        else:
            req = temporalio.api.workflowservice.v1.StartWorkflowExecutionRequest()
        req.namespace = self._client.namespace
        req.workflow_id = input.id
        req.workflow_type.name = input.workflow
        req.task_queue.name = input.task_queue
        if input.args:
            req.input.payloads.extend(
                await self._client.data_converter.encode(input.args)
            )
        if input.execution_timeout is not None:
            req.workflow_execution_timeout.FromTimedelta(input.execution_timeout)
        if input.run_timeout is not None:
            req.workflow_run_timeout.FromTimedelta(input.run_timeout)
        if input.task_timeout is not None:
            req.workflow_task_timeout.FromTimedelta(input.task_timeout)
        req.identity = self._client.identity
        req.request_id = str(uuid.uuid4())
        req.workflow_id_reuse_policy = cast(
            "temporalio.api.enums.v1.WorkflowIdReusePolicy.ValueType",
            int(input.id_reuse_policy),
        )
        if input.retry_policy is not None:
            input.retry_policy.apply_to_proto(req.retry_policy)
        req.cron_schedule = input.cron_schedule
        if input.memo is not None:
            for k, v in input.memo.items():
                req.memo.fields[k] = (await self._client.data_converter.encode([v]))[0]
        if input.search_attributes is not None:
            for k, v in input.search_attributes.items():
                req.search_attributes.indexed_fields[k] = (
                    await self._client.data_converter.encode([v])
                )[0]
        if input.header is not None:
            for k, v in input.header.items():
                req.header.fields[k] = (await self._client.data_converter.encode([v]))[
                    0
                ]

        # Start with signal or just normal start
        resp: Union[
            temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionResponse,
            temporalio.api.workflowservice.v1.StartWorkflowExecutionResponse,
        ]
        if isinstance(
            req,
            temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest,
        ):
            resp = await self._client.service.signal_with_start_workflow_execution(
                req, retry=True
            )
        else:
            resp = await self._client.service.start_workflow_execution(req, retry=True)
        return WorkflowHandle(self._client, req.workflow_id, run_id=resp.run_id)

    async def cancel_workflow(self, input: CancelWorkflowInput) -> None:
        await self._client.service.request_cancel_workflow_execution(
            temporalio.api.workflowservice.v1.RequestCancelWorkflowExecutionRequest(
                namespace=self._client.namespace,
                workflow_execution=temporalio.api.common.v1.WorkflowExecution(
                    workflow_id=input.id,
                    run_id=input.run_id or "",
                ),
                identity=self._client.identity,
                request_id=str(uuid.uuid4()),
                first_execution_run_id=input.first_execution_run_id or "",
            ),
            retry=True,
        )

    async def query_workflow(self, input: QueryWorkflowInput) -> Any:
        req = temporalio.api.workflowservice.v1.QueryWorkflowRequest(
            namespace=self._client.namespace,
            execution=temporalio.api.common.v1.WorkflowExecution(
                workflow_id=input.id,
                run_id=input.run_id or "",
            )
            # TODO(cretz): Headers here and elsewhere
        )
        if input.reject_condition:
            req.query_reject_condition = cast(
                "temporalio.api.enums.v1.QueryRejectCondition.ValueType",
                int(input.reject_condition),
            )
        req.query.query_type = input.query
        if input.args:
            req.query.query_args.payloads.extend(
                await self._client.data_converter.encode(input.args)
            )
        # TODO(cretz): Wrap error
        resp = await self._client.service.query_workflow(req, retry=True)
        if resp.HasField("query_rejected"):
            raise WorkflowQueryRejectedError(
                WorkflowExecutionStatus(resp.query_rejected.status)
                if resp.query_rejected.status
                else None
            )
        if not resp.query_result.payloads:
            return None
        results = await self._client.data_converter.decode(resp.query_result.payloads)
        if not results:
            return None
        elif len(results) > 1:
            logger.warning("Expected single query result, got %s", len(results))
        return results[0]

    async def signal_workflow(self, input: SignalWorkflowInput) -> None:
        req = temporalio.api.workflowservice.v1.SignalWorkflowExecutionRequest(
            namespace=self._client.namespace,
            workflow_execution=temporalio.api.common.v1.WorkflowExecution(
                workflow_id=input.id,
                run_id=input.run_id or "",
            ),
            signal_name=input.signal,
            identity=self._client.identity,
            request_id=str(uuid.uuid4()),
            # TODO(cretz): Headers here and elsewhere
        )
        if input.args:
            req.input.payloads.extend(
                await self._client.data_converter.encode(input.args)
            )
        await self._client.service.signal_workflow_execution(req, retry=True)

    async def terminate_workflow(self, input: TerminateWorkflowInput) -> None:
        req = temporalio.api.workflowservice.v1.TerminateWorkflowExecutionRequest(
            namespace=self._client.namespace,
            workflow_execution=temporalio.api.common.v1.WorkflowExecution(
                workflow_id=input.id,
                run_id=input.run_id or "",
            ),
            reason=input.reason or "",
            identity=self._client.identity,
            first_execution_run_id=input.first_execution_run_id or "",
        )
        if input.args:
            req.details.payloads.extend(
                await self._client.data_converter.encode(input.args)
            )
        await self._client.service.terminate_workflow_execution(req, retry=True)


class WorkflowFailureError(Exception):
    """Error that occurs when a workflow is unsuccessful."""

    def __init__(self, *, cause: temporalio.failure.FailureError) -> None:
        """Create workflow failure error."""
        super().__init__("Workflow execution failed")
        # TODO(cretz): Confirm setting this __cause__ is acceptable
        self.__cause__ = cause


class WorkflowContinuedAsNewError(Exception):
    """Error that occurs when a workflow was continued as new."""

    def __init__(self, new_execution_run_id: str) -> None:
        """Create workflow continue as new error."""
        super().__init__("Workflow continued as new")
        self._new_execution_run_id = new_execution_run_id


class WorkflowQueryRejectedError(Exception):
    """Error that occurs when a query was rejected."""

    def __init__(self, status: Optional[WorkflowExecutionStatus]) -> None:
        """Create workflow query rejected error."""
        super().__init__(f"Query rejected, status: {status}")
        self._status = status

    @property
    def status(self) -> Optional[WorkflowExecutionStatus]:
        """Get workflow execution status causing rejection."""
        return self._status
