"""Worker interceptor."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Optional, Type

import temporalio.activity
import temporalio.workflow


@dataclass
class ExecuteActivityInput:
    """Input for :py:meth:`ActivityInboundInterceptor.execute_activity`."""

    fn: Callable[..., Any]
    args: Iterable[Any]
    executor: Optional[concurrent.futures.Executor]
    _cancelled_event: temporalio.activity._CompositeEvent


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

    def intercept_workflow(
        self, next: WorkflowInboundInterceptor
    ) -> WorkflowInboundInterceptor:
        return next


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
class ExecuteWorkflowInput:
    type: Type
    # Note, this is an unbound method
    run_fn: Callable[..., Awaitable[Any]]
    args: Iterable[Any]


class WorkflowInboundInterceptor:
    def __init__(self, next: WorkflowInboundInterceptor) -> None:
        self.next = next

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        self.next.init(outbound)

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        return await self.next.execute_workflow(input)


class WorkflowOutboundInterceptor:
    def __init__(self, next: WorkflowOutboundInterceptor) -> None:
        self.next = next

    def info(self) -> temporalio.workflow.Info:
        return self.next.info()
