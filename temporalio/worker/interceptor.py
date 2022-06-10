"""Worker interceptor."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    List,
    Mapping,
    NoReturn,
    Optional,
    Type,
)

import temporalio.activity
import temporalio.common
import temporalio.workflow


class Interceptor:
    """Interceptor for workers.

    This should be extended by any worker interceptors.
    """

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Method called for intercepting an activity.

        Args:
            next: The underlying inbound interceptor this interceptor should
                delegate to.

        Returns:
            The new interceptor that will be used to for the activity.
        """
        return next

    def workflow_interceptor_class(self) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Class that will be instantiated and used to intercept workflows.

        The class must have the same init as
        :py:meth:`WorkflowInboundInterceptor.__init__`.

        Returns:
            The class to construct to intercept each workflow.
        """
        return None


@dataclass
class ExecuteActivityInput:
    """Input for :py:meth:`ActivityInboundInterceptor.execute_activity`."""

    fn: Callable[..., Any]
    args: Iterable[Any]
    executor: Optional[concurrent.futures.Executor]
    _cancelled_event: temporalio.activity._CompositeEvent


class ActivityInboundInterceptor:
    """Inbound interceptor to wrap outbound creation and activity execution.

    This should be extended by any activity inbound interceptors.
    """

    def __init__(self, next: ActivityInboundInterceptor) -> None:
        """Create the inbound interceptor.

        Args:
            next: The next interceptor in the chain. The default implementation
                of all calls is to delegate to the next interceptor.
        """
        self.next = next

    def init(self, outbound: ActivityOutboundInterceptor) -> None:
        """Initialize with an outbound interceptor.

        To add a custom outbound interceptor, wrap the given interceptor before
        sending to the next ``init`` call.
        """
        self.next.init(outbound)

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Called to invoke the activity."""
        return await self.next.execute_activity(input)


class ActivityOutboundInterceptor:
    """Outbound interceptor to wrap calls made from within activities.

    This should be extended by any activity outbound interceptors.
    """

    def __init__(self, next: ActivityOutboundInterceptor) -> None:
        """Create the outbound interceptor.

        Args:
            next: The next interceptor in the chain. The default implementation
                of all calls is to delegate to the next interceptor.
        """
        self.next = next

    def info(self) -> temporalio.activity.Info:
        """Called for every :py:func:`temporalio.activity.info` call."""
        return self.next.info()

    def heartbeat(self, *details: Any) -> None:
        """Called for every :py:func:`temporalio.activity.heartbeat` call."""
        self.next.heartbeat(*details)


@dataclass
class ContinueAsNewInput:
    """Input for :py:meth:`WorkflowOutboundInterceptor.continue_as_new`."""

    workflow: Optional[str]
    args: Iterable[Any]
    task_queue: Optional[str]
    run_timeout: Optional[timedelta]
    task_timeout: Optional[timedelta]
    memo: Optional[Mapping[str, Any]]
    search_attributes: Optional[temporalio.common.SearchAttributes]
    # The types may be absent
    arg_types: Optional[List[Type]]


@dataclass
class ExecuteWorkflowInput:
    """Input for :py:meth:`WorkflowInboundInterceptor.execute_workflow`."""

    type: Type
    # Note, this is an unbound method
    run_fn: Callable[..., Awaitable[Any]]
    args: Iterable[Any]


@dataclass
class GetExternalWorkflowHandleInput:
    """Input for :py:meth:`WorkflowOutboundInterceptor.get_external_workflow_handle`."""

    id: str
    run_id: Optional[str]


@dataclass
class HandleSignalInput:
    """Input for :py:meth:`WorkflowInboundInterceptor.handle_signal`."""

    name: str
    args: Iterable[Any]


@dataclass
class HandleQueryInput:
    """Input for :py:meth:`WorkflowInboundInterceptor.handle_query`."""

    id: str
    name: str
    args: Iterable[Any]


@dataclass
class StartActivityInput:
    """Input for :py:meth:`WorkflowOutboundInterceptor.start_activity`."""

    activity: str
    args: Iterable[Any]
    activity_id: Optional[str]
    task_queue: Optional[str]
    schedule_to_close_timeout: Optional[timedelta]
    schedule_to_start_timeout: Optional[timedelta]
    start_to_close_timeout: Optional[timedelta]
    heartbeat_timeout: Optional[timedelta]
    retry_policy: Optional[temporalio.common.RetryPolicy]
    cancellation_type: temporalio.workflow.ActivityCancellationType
    # The types may be absent
    arg_types: Optional[List[Type]]
    ret_type: Optional[Type]


@dataclass
class StartChildWorkflowInput:
    """Input for :py:meth:`WorkflowOutboundInterceptor.start_child_workflow`."""

    workflow: str
    args: Iterable[Any]
    id: str
    task_queue: Optional[str]
    namespace: Optional[str]
    cancellation_type: temporalio.workflow.ChildWorkflowCancellationType
    parent_close_policy: temporalio.workflow.ParentClosePolicy
    execution_timeout: Optional[timedelta]
    run_timeout: Optional[timedelta]
    task_timeout: Optional[timedelta]
    id_reuse_policy: temporalio.common.WorkflowIDReusePolicy
    retry_policy: Optional[temporalio.common.RetryPolicy]
    cron_schedule: str
    memo: Optional[Mapping[str, Any]]
    search_attributes: Optional[temporalio.common.SearchAttributes]
    # The types may be absent
    arg_types: Optional[List[Type]]
    ret_type: Optional[Type]


@dataclass
class StartLocalActivityInput:
    """Input for :py:meth:`WorkflowOutboundInterceptor.start_local_activity`."""

    activity: str
    args: Iterable[Any]
    activity_id: Optional[str]
    schedule_to_close_timeout: Optional[timedelta]
    schedule_to_start_timeout: Optional[timedelta]
    start_to_close_timeout: Optional[timedelta]
    retry_policy: Optional[temporalio.common.RetryPolicy]
    local_retry_threshold: Optional[timedelta]
    cancellation_type: temporalio.workflow.ActivityCancellationType
    # The types may be absent
    arg_types: Optional[List[Type]]
    ret_type: Optional[Type]


class WorkflowInboundInterceptor:
    """Inbound interceptor to wrap outbound creation, workflow execution, and
    signal/query handling.

    This should be extended by any workflow inbound interceptors.
    """

    def __init__(self, next: WorkflowInboundInterceptor) -> None:
        """Create the inbound interceptor.

        Args:
            next: The next interceptor in the chain. The default implementation
                of all calls is to delegate to the next interceptor.
        """
        self.next = next

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        """Initialize with an outbound interceptor.

        To add a custom outbound interceptor, wrap the given interceptor before
        sending to the next ``init`` call.
        """
        self.next.init(outbound)

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Called to run the workflow."""
        return await self.next.execute_workflow(input)

    async def handle_signal(self, input: HandleSignalInput) -> None:
        """Called to handle a signal."""
        return await self.next.handle_signal(input)

    async def handle_query(self, input: HandleQueryInput) -> Any:
        """Called to handle a query."""
        return await self.next.handle_query(input)


class WorkflowOutboundInterceptor:
    """Outbound interceptor to wrap calls made from within workflows.

    This should be extended by any workflow outbound interceptors.
    """

    def __init__(self, next: WorkflowOutboundInterceptor) -> None:
        """Create the outbound interceptor.

        Args:
            next: The next interceptor in the chain. The default implementation
                of all calls is to delegate to the next interceptor.
        """
        self.next = next

    def continue_as_new(self, input: ContinueAsNewInput) -> NoReturn:
        """Called for every :py:func:`temporalio.workflow.continue_as_new` call."""
        self.next.continue_as_new(input)

    def get_external_workflow_handle(
        self, input: GetExternalWorkflowHandleInput
    ) -> temporalio.workflow.ExternalWorkflowHandle:
        """Called for every
        :py:func:`temporalio.workflow.get_external_workflow_handle` and
        :py:func:`temporalio.workflow.get_external_workflow_handle_for` call.
        """
        return self.next.get_external_workflow_handle(input)

    def info(self) -> temporalio.workflow.Info:
        """Called for every :py:func:`temporalio.workflow.info` call."""
        return self.next.info()

    def start_activity(
        self, input: StartActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        """Called for every :py:func:`temporalio.workflow.start_activity` and
        :py:func:`temporalio.workflow.execute_activity` call.
        """
        return self.next.start_activity(input)

    async def start_child_workflow(
        self, input: StartChildWorkflowInput
    ) -> temporalio.workflow.ChildWorkflowHandle:
        """Called for every :py:func:`temporalio.workflow.start_child_workflow`
        and :py:func:`temporalio.workflow.execute_child_workflow` call.
        """
        return await self.next.start_child_workflow(input)

    def start_local_activity(
        self, input: StartLocalActivityInput
    ) -> temporalio.workflow.ActivityHandle:
        """Called for every :py:func:`temporalio.workflow.start_local_activity`
        and :py:func:`temporalio.workflow.execute_local_activity` call.
        """
        return self.next.start_local_activity(input)
