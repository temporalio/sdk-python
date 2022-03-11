"""Client for accessing Temporal."""

from __future__ import annotations

import logging
import uuid
import warnings
from dataclasses import dataclass
from datetime import timedelta
from enum import IntEnum
from typing import (
    Any,
    Callable,
    Generic,
    Iterable,
    Mapping,
    Optional,
    Tuple,
    TypeVar,
    Union,
    cast,
    overload,
)

import typing_extensions

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.failure.v1
import temporalio.api.history.v1
import temporalio.api.taskqueue.v1
import temporalio.api.workflowservice.v1
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow_service
from temporalio.workflow_service import RetryConfig, RPCError, RPCStatusCode, TLSConfig

logger = logging.getLogger(__name__)


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
        data_converter: temporalio.converter.DataConverter = temporalio.converter.default(),
        interceptors: Iterable[
            Union[Interceptor, Callable[[OutboundInterceptor], OutboundInterceptor]]
        ] = [],
        default_workflow_query_reject_condition: Optional[
            temporalio.common.QueryRejectCondition
        ] = None,
        type_hint_eval_str: bool = True,
        tls_config: Optional[TLSConfig] = None,
        retry_config: Optional[RetryConfig] = None,
        static_headers: Mapping[str, str] = {},
        identity: Optional[str] = None,
        worker_binary_id: Optional[str] = None,
    ) -> Client:
        """Connect to a Temporal server.

        Args:
            target_url: URL for the Temporal server. For local development, this
                is often "http://localhost:7233".
            namespace: Namespace to use for client calls.
            data_converter: Data converter to use for all data conversions
                to/from payloads.
            interceptors: Set of interceptors that are chained together to allow
                intercepting of client calls. The earlier interceptors wrap the
                later ones.

                Any interceptors that also implement
                :py:class:`temporalio.worker.Interceptor` will be used as worker
                interceptors too so they should not be given when creating a
                worker.
            default_workflow_query_reject_condition: The default rejection
                condition for workflow queries if not set during query. See
                :py:meth:`WorkflowHandle.query` for details on the rejection
                condition.
            type_hint_eval_str: Whether the type hinting that is used to
                determine dataclass parameters for decoding uses evaluation on
                stringified annotations. This corresponds to the eval_str
                parameter on :py:meth:`inspect.get_annotations`.
            tls_config: TLS configuration for connecting to the server. If unset
                no TLS connection will be used.
            retry_config: Retry configuration for direct service calls (when
                opted in) or all high-level calls made by this client (which all
                opt-in to retries by default). If unset, a default retry
                configuration is used.
            static_headers: Static headers to use for all calls to the server.
            identity: Identity for this client. If unset, a default is created
                based on the version of the SDK.
            worker_binary_id: Unique identifier for the current runtime. This is
                best set as a hash of all code and should change only when code
                does. If unset, a best-effort identifier is generated.
        """
        connect_config = temporalio.workflow_service.ConnectConfig(
            target_url=target_url,
            tls_config=tls_config,
            retry_config=retry_config,
            static_headers=static_headers,
            identity=identity or "",
            worker_binary_id=worker_binary_id or "",
        )
        return Client(
            await temporalio.workflow_service.WorkflowService.connect(connect_config),
            namespace=namespace,
            data_converter=data_converter,
            interceptors=interceptors,
            default_workflow_query_reject_condition=default_workflow_query_reject_condition,
            type_hint_eval_str=type_hint_eval_str,
        )

    def __init__(
        self,
        service: temporalio.workflow_service.WorkflowService,
        *,
        namespace: str = "default",
        data_converter: temporalio.converter.DataConverter = temporalio.converter.default(),
        interceptors: Iterable[
            Union[Interceptor, Callable[[OutboundInterceptor], OutboundInterceptor]]
        ] = [],
        default_workflow_query_reject_condition: Optional[
            temporalio.common.QueryRejectCondition
        ] = None,
        type_hint_eval_str: bool = True,
    ):
        """Create a Temporal client from a workflow service.

        See :py:meth:`connect` for details on the parameters.
        """
        # Iterate over interceptors in reverse building the impl
        self._impl: OutboundInterceptor = _ClientImpl(self)
        for interceptor in reversed(list(interceptors)):
            if isinstance(interceptor, Interceptor):
                self._impl = interceptor.intercept_client(self._impl)
            elif callable(interceptor):
                self._impl = interceptor(self._impl)
            else:
                raise TypeError("interceptor neither OutboundInterceptor nor callable")

        # Store the config for tracking
        self._config = ClientConfig(
            service=service,
            namespace=namespace,
            data_converter=data_converter,
            interceptors=interceptors,
            default_workflow_query_reject_condition=default_workflow_query_reject_condition,
            type_hint_eval_str=type_hint_eval_str,
        )

    def config(self) -> ClientConfig:
        """Config, as a dictionary, used to create this client.

        This makes a shallow copy of the config each call.
        """
        config = self._config.copy()
        config["interceptors"] = list(config["interceptors"])
        return config

    @property
    def service(self) -> temporalio.workflow_service.WorkflowService:
        """Raw gRPC service for this client."""
        return self._config["service"]

    @property
    def namespace(self) -> str:
        """Namespace used in calls by this client."""
        return self._config["namespace"]

    @property
    def identity(self) -> str:
        """Identity used in calls by this client."""
        return self.service.config.identity

    @property
    def data_converter(self) -> temporalio.converter.DataConverter:
        """Data converter used by this client."""
        return self._config["data_converter"]

    async def start_workflow(
        self,
        workflow: str,
        *args: Any,
        id: str,
        task_queue: str,
        execution_timeout: Optional[timedelta] = None,
        run_timeout: Optional[timedelta] = None,
        task_timeout: Optional[timedelta] = None,
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        retry_policy: Optional[temporalio.common.RetryPolicy] = None,
        cron_schedule: str = "",
        memo: Optional[Mapping[str, Any]] = None,
        search_attributes: Optional[Mapping[str, Any]] = None,
        header: Optional[Mapping[str, Any]] = None,
        start_signal: Optional[str] = None,
        start_signal_args: Iterable[Any] = [],
    ) -> WorkflowHandle[Any]:
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

        Raises:
            RPCError: Workflow could not be started.
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
        id_reuse_policy: temporalio.common.WorkflowIDReusePolicy = temporalio.common.WorkflowIDReusePolicy.ALLOW_DUPLICATE,
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
        self,
        workflow_id: str,
        *,
        run_id: Optional[str] = None,
        first_execution_run_id: Optional[str] = None,
    ) -> WorkflowHandle[Any]:
        """Get a workflow handle to an existing workflow by its ID.

        Args:
            workflow_id: Workflow ID to get a handle to.
            run_id: Run ID that will be used for all calls.
            first_execution_run_id: First execution run ID used for cancellation
                and termination.

        Returns:
            The workflow handle.
        """
        return WorkflowHandle(
            self,
            workflow_id,
            run_id=run_id,
            result_run_id=run_id,
            first_execution_run_id=first_execution_run_id,
        )

    @overload
    def get_activity_completion_handle(
        self, *, workflow_id: str, run_id: str, activity_id: str
    ) -> ActivityCompletionHandle:
        pass

    @overload
    def get_activity_completion_handle(
        self, *, task_token: bytes
    ) -> ActivityCompletionHandle:
        pass

    def get_activity_completion_handle(
        self,
        *,
        workflow_id: Optional[str] = None,
        run_id: Optional[str] = None,
        activity_id: Optional[str] = None,
        task_token: Optional[bytes] = None,
    ) -> ActivityCompletionHandle:
        """Get an activity completion handle."""
        if task_token is not None:
            if workflow_id is not None or run_id is not None or activity_id is not None:
                raise ValueError("Task token cannot be present with other IDs")
            return ActivityCompletionHandle(id_or_token=task_token)
        elif workflow_id is not None:
            if run_id is None or activity_id is None:
                raise ValueError(
                    "Workflow ID, run ID, and activity ID must all be given together"
                )
            return ActivityCompletionHandle(
                id_or_token=(workflow_id, run_id, activity_id)
            )
        raise ValueError("Task token or workflow/run/activity ID must be present")


class ClientConfig(typing_extensions.TypedDict, total=False):
    """TypedDict of config originally passed to :py:meth:`Client`."""

    service: temporalio.workflow_service.WorkflowService
    namespace: str
    data_converter: temporalio.converter.DataConverter
    interceptors: Iterable[
        Union[Interceptor, Callable[[OutboundInterceptor], OutboundInterceptor]]
    ]
    default_workflow_query_reject_condition: Optional[
        temporalio.common.QueryRejectCondition
    ]
    type_hint_eval_str: bool


T = TypeVar("T")


class WorkflowHandle(Generic[T]):
    """Handle for interacting with a workflow.

    This is usually created via :py:meth:`Client.get_workflow_handle` or
    returned from :py:meth:`Client.start_workflow`.
    """

    def __init__(
        self,
        client: Client,
        id: str,
        *,
        run_id: Optional[str] = None,
        result_run_id: Optional[str] = None,
        first_execution_run_id: Optional[str] = None,
    ) -> None:
        """Create workflow handle."""
        self._client = client
        self._id = id
        self._run_id = run_id
        self._result_run_id = result_run_id
        self._first_execution_run_id = first_execution_run_id

    @property
    def id(self) -> str:
        """ID for the workflow."""
        return self._id

    @property
    def run_id(self) -> Optional[str]:
        """Run ID used for :py:meth:`signal` and :py:meth:`query` calls if
        present to ensure the query or signal happen on this exact run.

        This is only created via :py:meth:`Client.get_workflow_handle`.
        :py:meth:`Client.start_workflow` will not set this value.

        This cannot be mutated. If a different run ID is needed,
        :py:meth:`Client.get_workflow_handle` must be used instead.
        """
        return self._run_id

    @property
    def result_run_id(self) -> Optional[str]:
        """Run ID used for :py:meth:`result` calls if present to ensure result
        is for a workflow starting from this run.

        When this handle is created via :py:meth:`Client.get_workflow_handle`,
        this is the same as run_id. When this handle is created via
        :py:meth:`Client.start_workflow`, this value will be the resulting run
        ID.

        This cannot be mutated. If a different run ID is needed,
        :py:meth:`Client.get_workflow_handle` must be used instead.
        """
        return self._result_run_id

    @property
    def first_execution_run_id(self) -> Optional[str]:
        """Run ID used for :py:meth:`cancel` and :py:meth:`terminate` calls if
        present to ensure the cancel and terminate happen for a workflow ID
        started with this run ID.

        This can be set when using :py:meth:`Client.get_workflow_handle`. When
        :py:meth:`Client.start_workflow` is called without a start signal, this
        is set to the resulting run.

        This cannot be mutated. If a different first execution run ID is needed,
        :py:meth:`Client.get_workflow_handle` must be used instead.
        """
        return self._first_execution_run_id

    async def result(self, *, follow_runs: bool = True) -> T:
        """Wait for result of the workflow.

        This will use :py:attr:`result_run_id` if present to base the result on.
        To use another run ID, a new handle must be created via
        :py:meth:`Client.get_workflow_handle`.

        Args:
            follow_runs: If true (default), workflow runs will be continually
                fetched, until the most recent one is found. If false, the first
                result is used.

        Returns:
            Result of the workflow after being converted by the data converter.

        Raises:
            WorkflowFailureError: Workflow failed, was cancelled, was
                terminated, or timed out. Use the
                :py:attr:`WorkflowFailureError.cause` to see the underlying
                reason.
            Exception: Other possible failures during result fetching.
        """
        req = temporalio.api.workflowservice.v1.GetWorkflowExecutionHistoryRequest(
            namespace=self._client.namespace,
            execution=temporalio.api.common.v1.WorkflowExecution(
                workflow_id=self._id, run_id=self._result_run_id or ""
            ),
            wait_new_event=True,
            history_event_filter_type=temporalio.api.enums.v1.HistoryEventFilterType.HISTORY_EVENT_FILTER_TYPE_CLOSE_EVENT,
            skip_archival=True,
        )
        while True:
            resp = await self._client.service.get_workflow_execution_history(
                req, retry=True
            )
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
                results = await temporalio.converter.decode_payloads(
                    complete_attr.result, self._client.data_converter
                )
                if not results:
                    return cast(T, None)
                elif len(results) > 1:
                    warnings.warn(f"Expected single result, got {len(results)}")
                return cast(T, results[0])
            elif event.HasField("workflow_execution_failed_event_attributes"):
                fail_attr = event.workflow_execution_failed_event_attributes
                # Follow execution
                if follow_runs and fail_attr.new_execution_run_id:
                    req.execution.run_id = fail_attr.new_execution_run_id
                    req.next_page_token = b""
                    continue
                raise WorkflowFailureError(
                    cause=await temporalio.exceptions.failure_to_error(
                        fail_attr.failure, self._client.data_converter
                    ),
                )
            elif event.HasField("workflow_execution_canceled_event_attributes"):
                cancel_attr = event.workflow_execution_canceled_event_attributes
                raise WorkflowFailureError(
                    cause=temporalio.exceptions.CancelledError(
                        "Workflow cancelled",
                        *(
                            await temporalio.converter.decode_payloads(
                                cancel_attr.details,
                                self._client.data_converter,
                            )
                        ),
                    )
                )
            elif event.HasField("workflow_execution_terminated_event_attributes"):
                term_attr = event.workflow_execution_terminated_event_attributes
                raise WorkflowFailureError(
                    cause=temporalio.exceptions.TerminatedError(
                        term_attr.reason or "Workflow terminated",
                        *(
                            await temporalio.converter.decode_payloads(
                                term_attr.details,
                                self._client.data_converter,
                            )
                        ),
                    ),
                )
            elif event.HasField("workflow_execution_timed_out_event_attributes"):
                time_attr = event.workflow_execution_timed_out_event_attributes
                # Follow execution
                if follow_runs and time_attr.new_execution_run_id:
                    req.execution.run_id = time_attr.new_execution_run_id
                    req.next_page_token = b""
                    continue
                raise WorkflowFailureError(
                    cause=temporalio.exceptions.TimeoutError(
                        "Workflow timed out",
                        type=temporalio.exceptions.TimeoutType.START_TO_CLOSE,
                        last_heartbeat_details=[],
                    ),
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

    async def cancel(self) -> None:
        """Cancel the workflow.

        This will issue a cancellation for :py:attr:`run_id` if present. This
        call will make sure to use the run chain starting from
        :py:attr:`first_execution_run_id` if present. To create handles with
        these values, use :py:meth:`Client.get_workflow_handle`.

        .. warning::
            Handles created as a result of :py:meth:`Client.start_workflow` with
            a start signal will cancel the latest workflow with the same
            workflow ID even if it is unrelated to the started workflow.

        Raises:
            RPCError: Workflow could not be cancelled.
        """
        await self._client._impl.cancel_workflow(
            CancelWorkflowInput(
                id=self._id,
                run_id=self._run_id,
                first_execution_run_id=self._first_execution_run_id,
            )
        )

    async def describe(
        self,
    ) -> WorkflowDescription:
        """Get workflow details.

        This will get details for :py:attr:`run_id` if present. To use a
        different run ID, create a new handle with via
        :py:meth:`Client.get_workflow_handle`.

        .. warning::
            Handles created as a result of :py:meth:`Client.start_workflow` will
            describe the latest workflow with the same workflow ID even if it is
            unrelated to the started workflow.

        Returns:
            Workflow details.

        Raises:
            RPCError: Workflow details could not be fetched.
        """
        return WorkflowDescription(
            await self._client.service.describe_workflow_execution(
                temporalio.api.workflowservice.v1.DescribeWorkflowExecutionRequest(
                    namespace=self._client.namespace,
                    execution=temporalio.api.common.v1.WorkflowExecution(
                        workflow_id=self._id,
                        run_id=self._run_id or "",
                    ),
                ),
                retry=True,
            )
        )

    async def query(
        self,
        query: str,
        *args: Any,
        reject_condition: Optional[temporalio.common.QueryRejectCondition] = None,
    ) -> Any:
        """Query the workflow.

        This will query for :py:attr:`run_id` if present. To use a different
        run ID, create a new handle with via
        :py:meth:`Client.get_workflow_handle`.

        .. warning::
            Handles created as a result of :py:meth:`Client.start_workflow` will
            query the latest workflow with the same workflow ID even if it is
            unrelated to the started workflow.

        Args:
            query: Query name on the workflow.
            args: Query arguments.
            reject_condition: Condition for rejecting the query. If unset/None,
                defaults to the client's default (which is defaulted to None).

        Returns:
            Result of the query.

        Raises:
            WorkflowQueryRejectedError: A query reject condition was satisfied.
            RPCError: Workflow details could not be fetched.
        """
        return await self._client._impl.query_workflow(
            QueryWorkflowInput(
                id=self._id,
                run_id=self._run_id,
                query=query,
                args=args,
                reject_condition=reject_condition
                or self._client._config["default_workflow_query_reject_condition"],
            )
        )

    async def signal(self, name: str, *args: Any) -> None:
        """Send a signal to the workflow.

        This will signal for :py:attr:`run_id` if present. To use a different
        run ID, create a new handle with via
        :py:meth:`Client.get_workflow_handle`.

        .. warning::
            Handles created as a result of :py:meth:`Client.start_workflow` will
            signal the latest workflow with the same workflow ID even if it is
            unrelated to the started workflow.

        Args:
            name: Signal name on the workflow.
            args: Signal arguments.
            run_id: Run ID to signal. Defaults to using :py:meth:`run_id`. If
                set to None or there is no :py:meth:`run_id`, this will query
                the latest run for the workflow ID.

        Raises:
            RPCError: Workflow could not be signalled.
        """
        await self._client._impl.signal_workflow(
            SignalWorkflowInput(
                id=self._id,
                run_id=self._run_id,
                signal=name,
                args=args,
            )
        )

    async def terminate(
        self,
        *args: Any,
        reason: Optional[str] = None,
    ) -> None:
        """Terminate the workflow.

        This will issue a termination for :py:attr:`run_id` if present. This
        call will make sure to use the run chain starting from
        :py:attr:`first_execution_run_id` if present. To create handles with
        these values, use :py:meth:`Client.get_workflow_handle`.

        .. warning::
            Handles created as a result of :py:meth:`Client.start_workflow` with
            a start signal will terminate the latest workflow with the same
            workflow ID even if it is unrelated to the started workflow.

        Args:
            args: Details to store on the termination.
            reason: Reason for the termination.

        Raises:
            RPCError: Workflow could not be terminated.
        """
        await self._client._impl.terminate_workflow(
            TerminateWorkflowInput(
                id=self._id,
                run_id=self._run_id,
                args=args,
                reason=reason,
                first_execution_run_id=self._first_execution_run_id,
            )
        )


class ActivityCompletionHandle:
    """Handle representing an external activity for completion and heartbeat."""

    def __init__(self, id_or_token: Union[Tuple[str, str, str], bytes]) -> None:
        """Create an activity completion handle."""
        raise NotImplementedError

    # TODO(cretz): The async methods here and the interceptor methods to support
    # them


class WorkflowDescription:
    """Description for a workflow."""

    def __init__(
        self,
        raw_message: temporalio.api.workflowservice.v1.DescribeWorkflowExecutionResponse,
    ):
        """Create a workflow description from a describe response."""
        self._raw_message = raw_message
        status = raw_message.workflow_execution_info.status
        self._status = WorkflowExecutionStatus(status) if status else None

    @property
    def raw_message(
        self,
    ) -> temporalio.api.workflowservice.v1.DescribeWorkflowExecutionResponse:
        """Underlying workflow description response."""
        return self._raw_message

    @property
    def status(self) -> Optional[WorkflowExecutionStatus]:
        """Status of the workflow."""
        return self._status


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


class WorkflowFailureError(Exception):
    """Error that occurs when a workflow is unsuccessful."""

    def __init__(self, *, cause: temporalio.exceptions.FailureError) -> None:
        """Create workflow failure error."""
        super().__init__("Workflow execution failed")
        self.__cause__ = cause

    @property
    def cause(self) -> temporalio.exceptions.FailureError:
        """Cause of the workflow failure."""
        return cast(temporalio.exceptions.FailureError, self.__cause__)


class WorkflowContinuedAsNewError(temporalio.exceptions.TemporalError):
    """Error that occurs when a workflow was continued as new."""

    def __init__(self, new_execution_run_id: str) -> None:
        """Create workflow continue as new error."""
        super().__init__("Workflow continued as new")
        self._new_execution_run_id = new_execution_run_id

    @property
    def new_execution_run_id(self) -> str:
        """New execution run ID the workflow continued to"""
        return self._new_execution_run_id


class WorkflowQueryRejectedError(temporalio.exceptions.TemporalError):
    """Error that occurs when a query was rejected."""

    def __init__(self, status: Optional[WorkflowExecutionStatus]) -> None:
        """Create workflow query rejected error."""
        super().__init__(f"Query rejected, status: {status}")
        self._status = status

    @property
    def status(self) -> Optional[WorkflowExecutionStatus]:
        """Get workflow execution status causing rejection."""
        return self._status


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
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy
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
    reject_condition: Optional[temporalio.common.QueryRejectCondition]


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

    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
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

    def __init__(self, next: OutboundInterceptor) -> None:
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
        first_execution_run_id = None
        if isinstance(
            req,
            temporalio.api.workflowservice.v1.SignalWithStartWorkflowExecutionRequest,
        ):
            resp = await self._client.service.signal_with_start_workflow_execution(
                req, retry=True
            )
        else:
            resp = await self._client.service.start_workflow_execution(req, retry=True)
            first_execution_run_id = resp.run_id
        return WorkflowHandle(
            self._client,
            req.workflow_id,
            result_run_id=resp.run_id,
            first_execution_run_id=first_execution_run_id,
        )

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
            warnings.warn(f"Expected single query result, got {len(results)}")
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
