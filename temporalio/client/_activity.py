"""Client support for accessing Temporal."""

from __future__ import annotations

import asyncio
import functools
import warnings
from collections.abc import (
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    cast,
)

from typing_extensions import Self

import temporalio.api.activity.v1
import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.failure.v1
import temporalio.api.workflowservice.v1
import temporalio.common
import temporalio.converter
import temporalio.converter._search_attributes
from temporalio.converter import (
    ActivitySerializationContext,
    DataConverter,
    SerializationContext,
    WithSerializationContext,
)
from temporalio.service import (
    RPCError,
    RPCStatusCode,
)

from ..types import (
    ReturnType,
)
from ._exceptions import ActivityFailureError
from ._interceptor import (
    CancelActivityInput,
    CompleteAsyncActivityInput,
    DescribeActivityInput,
    FailAsyncActivityInput,
    HeartbeatAsyncActivityInput,
    ReportCancellationAsyncActivityInput,
    TerminateActivityInput,
)

if TYPE_CHECKING:
    from ._client import Client
    from ._interceptor import ListActivitiesInput


class ActivityExecutionAsyncIterator:
    """Asynchronous iterator for activity execution values.

    You should typically use ``async for`` on this iterator and not call any of its methods.

    .. warning::
       This API is experimental.
    """

    def __init__(
        self,
        client: Client,
        input: ListActivitiesInput,
    ) -> None:
        """Create an asynchronous iterator for the given input.

        Users should not create this directly, but rather use
        :py:meth:`Client.list_activities`.
        """
        self._client = client
        self._input = input
        self._next_page_token = input.next_page_token
        self._current_page: Sequence[ActivityExecution] | None = None
        self._current_page_index = 0
        self._limit = input.limit
        self._yielded = 0

    @property
    def current_page_index(self) -> int:
        """Index of the entry in the current page that will be returned from
        the next :py:meth:`__anext__` call.
        """
        return self._current_page_index

    @property
    def current_page(self) -> Sequence[ActivityExecution] | None:
        """Current page, if it has been fetched yet."""
        return self._current_page

    @property
    def next_page_token(self) -> bytes | None:
        """Token for the next page request if any."""
        return self._next_page_token

    async def fetch_next_page(self, *, page_size: int | None = None) -> None:
        """Fetch the next page of results.

        Args:
            page_size: Override the page size this iterator was originally
                created with.
        """
        page_size = page_size or self._input.page_size
        if self._limit is not None and self._limit - self._yielded < page_size:
            page_size = self._limit - self._yielded

        resp = await self._client.workflow_service.list_activity_executions(
            temporalio.api.workflowservice.v1.ListActivityExecutionsRequest(
                namespace=self._client.namespace,
                page_size=page_size,
                next_page_token=self._next_page_token or b"",
                query=self._input.query or "",
            ),
            retry=True,
            metadata=self._input.rpc_metadata,
            timeout=self._input.rpc_timeout,
        )

        self._current_page = [
            ActivityExecution._from_raw_info(v, self._client.namespace)
            for v in resp.executions
        ]
        self._current_page_index = 0
        self._next_page_token = resp.next_page_token or None

    def __aiter__(self) -> ActivityExecutionAsyncIterator:
        """Return self as the iterator."""
        return self

    async def __anext__(self) -> ActivityExecution:
        """Get the next execution on this iterator, fetching next page if
        necessary.
        """
        if self._limit is not None and self._yielded >= self._limit:
            raise StopAsyncIteration
        while True:
            # No page? fetch and continue
            if self._current_page is None:
                await self.fetch_next_page()
                continue
            # No more left in page?
            if self._current_page_index >= len(self._current_page):
                # If there is a next page token, try to get another page and try
                # again
                if self._next_page_token is not None:
                    await self.fetch_next_page()
                    continue
                # No more pages means we're done
                raise StopAsyncIteration
            # Get current, increment page index, and return
            ret = self._current_page[self._current_page_index]
            self._current_page_index += 1
            self._yielded += 1
            return ret


@dataclass(eq=False, kw_only=True)
class ActivityExecution:
    """Info for an activity execution not started by a workflow, from list response.

    .. warning::
       This API is experimental.
    """

    activity_id: str
    """Activity ID."""

    activity_run_id: str | None
    """Run ID of the activity."""

    activity_type: str
    """Type name of the activity."""

    close_time: datetime | None
    """Time the activity reached a terminal status, if closed."""

    execution_duration: timedelta | None
    """Duration from schedule to close time, only populated if closed."""

    execution_time: datetime | None
    """The time at which the first activity task is made available for dispatch, computed as schedule time + start delay."""

    namespace: str
    """Namespace of the activity (copied from calling client)."""

    schedule_time: datetime
    """Time the activity was originally scheduled."""

    status: ActivityExecutionStatus
    """Current status of the activity."""

    task_queue: str
    """Task queue the activity was scheduled on."""

    typed_search_attributes: temporalio.common.TypedSearchAttributes
    """Current set of search attributes if any."""

    raw_info: (
        temporalio.api.activity.v1.ActivityExecutionListInfo
        | temporalio.api.activity.v1.ActivityExecutionInfo
    ) = field(repr=False)
    """Underlying protobuf info."""

    @classmethod
    def _from_raw_info(
        cls,
        info: temporalio.api.activity.v1.ActivityExecutionListInfo,
        namespace: str,
        **kwargs,
    ) -> Self:
        """Create from raw proto activity list info."""
        return cls(
            raw_info=info,
            activity_id=info.activity_id,
            activity_run_id=info.run_id or None,
            activity_type=info.activity_type.name,
            close_time=(
                info.close_time.ToDatetime().replace(tzinfo=timezone.utc)
                if info.HasField("close_time")
                else None
            ),
            execution_duration=(
                info.execution_duration.ToTimedelta()
                if info.HasField("execution_duration")
                else None
            ),
            execution_time=(
                info.execution_time.ToDatetime().replace(tzinfo=timezone.utc)
                if info.HasField("execution_time")
                else None
            ),
            namespace=namespace,
            schedule_time=(
                info.schedule_time.ToDatetime().replace(tzinfo=timezone.utc)
                if info.HasField("schedule_time")
                else datetime.min
            ),
            status=(
                ActivityExecutionStatus(info.status)
                if info.status
                else ActivityExecutionStatus.UNSPECIFIED
            ),
            task_queue=info.task_queue,
            typed_search_attributes=temporalio.converter.decode_typed_search_attributes(
                info.search_attributes
            ),
            **kwargs,
        )


@dataclass(eq=False, kw_only=True)
class ActivityExecutionDescription(ActivityExecution):
    """Detailed information about an activity execution not started by a workflow.

    .. warning::
       This API is experimental.
    """

    attempt: int
    """Current attempt number."""

    canceled_reason: str | None
    """Reason for cancellation, if cancel was requested."""

    close_time: datetime | None
    """Time when the activity transitioned to a closed state."""

    current_retry_interval: timedelta | None
    """Time until the next retry, if applicable."""

    expiration_time: datetime | None
    """The time at which the activity's schedule-to-close timeout expires."""

    heartbeat_timeout: timedelta | None
    """Configured heartbeat timeout of the activity."""

    last_attempt_complete_time: datetime | None
    """Time when the last attempt completed."""

    last_deployment_version: temporalio.common.WorkerDeploymentVersion | None
    """The Worker Deployment Version this activity was dispatched to most recently."""

    last_heartbeat_time: datetime | None
    """Time of the last heartbeat."""

    last_started_time: datetime | None
    """Time the last attempt was started."""

    last_worker_identity: str | None
    """Identity of the last worker that processed the activity."""

    next_attempt_schedule_time: datetime | None
    """Time when the next attempt will be scheduled."""

    priority: temporalio.common.Priority
    """Priority metadata."""

    retry_policy: temporalio.common.RetryPolicy | None
    """Retry policy for the activity."""

    run_state: PendingActivityState | None
    """More detailed breakdown if status is RUNNING."""

    schedule_to_close_timeout: timedelta | None
    """Configured schedule-to-close timeout of the activity."""

    schedule_to_start_timeout: timedelta | None
    """Configured schedule-to-start timeout of the activity."""

    start_to_close_timeout: timedelta | None
    """Configured start-to-close timeout of the activity."""

    start_delay: timedelta | None
    """Time to wait before making the first activity task available for dispatch."""

    total_heartbeat_count: int
    """Total number of heartbeats recorded across all attempts of this activity, including retries.

    Zero if the activity has not sent any heartbeats or if the server didn't report heartbeat count.
    """

    raw_info: temporalio.api.activity.v1.ActivityExecutionInfo = field(repr=False)
    """Underlying protobuf info."""

    raw_callbacks: Sequence[temporalio.api.activity.v1.CallbackInfo] = field(repr=False)
    """Underlying protobuf callbacks"""

    raw_input: temporalio.api.common.v1.Payloads | None = field(repr=False)
    """Raw input of the activity. Use :py:meth:`input` to decode."""

    raw_outcome: temporalio.api.activity.v1.ActivityExecutionOutcome | None = field(
        repr=False
    )
    """Raw outcome of the activity. Use :py:meth:`outcome` to decode."""

    data_converter: DataConverter = field(repr=False)
    """Data converter used to convert raw payloads. By default it's the same as the client's data converter."""

    @classmethod
    def _from_resp(
        cls,
        resp: temporalio.api.workflowservice.v1.DescribeActivityExecutionResponse,
        namespace: str,
        data_converter: temporalio.converter.DataConverter,
        **kwargs,
    ) -> Self:
        """Create from raw proto activity execution info."""
        return cls._from_raw_info(
            info=resp.info,
            namespace=namespace,
            attempt=resp.info.attempt,
            canceled_reason=resp.info.canceled_reason or None,
            current_retry_interval=(
                resp.info.current_retry_interval.ToTimedelta()
                if resp.info.HasField("current_retry_interval")
                else None
            ),
            expiration_time=(
                resp.info.expiration_time.ToDatetime(tzinfo=timezone.utc)
                if resp.info.HasField("expiration_time")
                else datetime.min
            ),
            heartbeat_timeout=(
                resp.info.heartbeat_timeout.ToTimedelta()
                if resp.info.HasField("heartbeat_timeout")
                else None
            ),
            last_attempt_complete_time=(
                resp.info.last_attempt_complete_time.ToDatetime(tzinfo=timezone.utc)
                if resp.info.HasField("last_attempt_complete_time")
                else None
            ),
            last_deployment_version=(
                temporalio.common.WorkerDeploymentVersion(
                    deployment_name=resp.info.last_deployment_version.deployment_name,
                    build_id=resp.info.last_deployment_version.build_id,
                )
                if resp.info.HasField("last_deployment_version")
                else None
            ),
            last_heartbeat_time=(
                resp.info.last_heartbeat_time.ToDatetime(tzinfo=timezone.utc)
                if resp.info.HasField("last_heartbeat_time")
                else None
            ),
            last_started_time=(
                resp.info.last_started_time.ToDatetime(tzinfo=timezone.utc)
                if resp.info.HasField("last_started_time")
                else None
            ),
            last_worker_identity=resp.info.last_worker_identity or None,
            next_attempt_schedule_time=(
                resp.info.next_attempt_schedule_time.ToDatetime(tzinfo=timezone.utc)
                if resp.info.HasField("next_attempt_schedule_time")
                else None
            ),
            priority=temporalio.common.Priority._from_proto(resp.info.priority),
            retry_policy=(
                temporalio.common.RetryPolicy.from_proto(resp.info.retry_policy)
                if resp.info.HasField("retry_policy")
                else None
            ),
            run_state=(
                PendingActivityState(resp.info.run_state)
                if resp.info.run_state
                else None
            ),
            schedule_to_close_timeout=(
                resp.info.schedule_to_close_timeout.ToTimedelta()
                if resp.info.HasField("schedule_to_close_timeout")
                else None
            ),
            schedule_to_start_timeout=(
                resp.info.schedule_to_start_timeout.ToTimedelta()
                if resp.info.HasField("schedule_to_start_timeout")
                else None
            ),
            start_to_close_timeout=(
                resp.info.start_to_close_timeout.ToTimedelta()
                if resp.info.HasField("start_to_close_timeout")
                else None
            ),
            start_delay=(
                resp.info.start_delay.ToTimedelta()
                if resp.info.HasField("start_delay")
                else None
            ),
            total_heartbeat_count=resp.info.total_heartbeat_count,
            raw_callbacks=resp.callbacks,
            raw_input=resp.input if resp.HasField("input") else None,
            raw_outcome=resp.outcome if resp.HasField("outcome") else None,
            data_converter=data_converter,
            **kwargs,
        )

    def has_heartbeat_details(self) -> bool:
        """True if heartbeat details are available. Use :py:meth:`heartbeat_details` to retrieve them.

        Always false if `include_heartbeat_details` was false in the `describe` call.
        """
        return self.raw_info.HasField("heartbeat_details")

    async def heartbeat_details(
        self, type_hints: list[type] | None = None
    ) -> list[Any] | None:
        """Returns details from the last heartbeat, or `None` if not available.

        Always `None` if `include_heartbeat_details` was false in the `describe` call.
        Type hints can be provided to aid data conversion.
        """
        return (
            await self.data_converter.decode_wrapper(
                self.raw_info.heartbeat_details, type_hints
            )
            if self.has_heartbeat_details()
            else None
        )

    def has_last_failure(self) -> bool:
        """True if last failure is available. Use :py:meth:`last_failure` to retrieve it.

        Always false if `include_heartbeat_details` was false in the `describe` call.
        """
        return self.raw_info.HasField("last_failure")

    async def last_failure(self) -> BaseException | None:
        """Returns failure from the last failed attempt, or `None` if not available.

        Always `None` if `include_last_failure` was false in the `describe` call.
        """
        return (
            await self.data_converter.decode_failure(self.raw_info.last_failure)
            if self.has_last_failure()
            else None
        )

    def has_input(self) -> bool:
        """True if activity input is available. Use :py:meth:`input` to retrieve it.

        Always false if `include_input` was false in the `describe` call.
        """
        return self.raw_input is not None

    async def input(self, type_hints: list[type] | None = None) -> list[Any] | None:
        """Returns activity input, or `None` if not available.

        Always `None` if `include_input` was false in the `describe` call.
        Type hints can be provided to aid data conversion.
        """
        return (
            await self.data_converter.decode_wrapper(self.raw_input, type_hints)
            if self.has_input()
            else None
        )

    def has_result(self) -> bool:
        """True if activity result is available. Use :py:meth:`result` to retrieve it.

        Activity result is only available if the activity has completed and was successful.
        Always false if `include_outcome` was false in the `describe` call.
        """
        return self.raw_outcome is not None and self.raw_outcome.HasField("result")

    async def result(self, type_hint: type | None = None) -> Any | None:
        """Returns activity result, or `None` if not available.

        Activity result is only available if the activity has completed successfully.
        Always false if `include_outcome` was false in the `describe` call.
        Type hints can be provided to aid data conversion.
        """
        if not self.has_result():
            return None
        type_hints = [type_hint] if type_hint is not None else None
        results = await self.data_converter.decode_wrapper(
            self.raw_outcome.result, type_hints
        )
        if not results:
            return None
        if len(results) > 1:
            warnings.warn(f"Expected single activity result, got {len(results)}")
        return results[0]

    def has_outcome_failure(self) -> bool:
        """True if activity outcome failure is available. Use :py:meth:`outcome_failure` to retrieve it.

        Activity outcome failure is only available if the activity has closed with a failure.
        Use :py:meth:`last_failure` to retrieve failure of the most recent failed attempt of an activity that's still
        running or that completed successfully.
        Always false if `include_outcome` was false in the `describe` call.
        """
        return self.raw_outcome is not None and self.raw_outcome.HasField("failure")

    async def outcome_failure(self) -> BaseException | None:
        """Returns activity outcome failure, or `None` if not available.

        Activity outcome failure is only available if the activity has closed with a failure.
        Use :py:meth:`last_failure` to retrieve failure of the most recent failed attempt of an activity that's still
        running or that completed successfully.
        Always false if `include_outcome` was false in the `describe` call.
        """
        return (
            await self.data_converter.decode_failure(self.raw_outcome.failure)
            if self.has_outcome_failure()
            else None
        )


class ActivityExecutionStatus(IntEnum):
    """Status of an activity execution.

    .. warning::
       This API is experimental.

    See :py:class:`temporalio.api.enums.v1.ActivityExecutionStatus`.
    """

    UNSPECIFIED = int(
        temporalio.api.enums.v1.ActivityExecutionStatus.ACTIVITY_EXECUTION_STATUS_UNSPECIFIED
    )
    RUNNING = int(
        temporalio.api.enums.v1.ActivityExecutionStatus.ACTIVITY_EXECUTION_STATUS_RUNNING
    )
    COMPLETED = int(
        temporalio.api.enums.v1.ActivityExecutionStatus.ACTIVITY_EXECUTION_STATUS_COMPLETED
    )
    FAILED = int(
        temporalio.api.enums.v1.ActivityExecutionStatus.ACTIVITY_EXECUTION_STATUS_FAILED
    )
    CANCELED = int(
        temporalio.api.enums.v1.ActivityExecutionStatus.ACTIVITY_EXECUTION_STATUS_CANCELED
    )
    TERMINATED = int(
        temporalio.api.enums.v1.ActivityExecutionStatus.ACTIVITY_EXECUTION_STATUS_TERMINATED
    )
    TIMED_OUT = int(
        temporalio.api.enums.v1.ActivityExecutionStatus.ACTIVITY_EXECUTION_STATUS_TIMED_OUT
    )


class PendingActivityState(IntEnum):
    """Detailed state of an activity execution that is in ACTIVITY_EXECUTION_STATUS_RUNNING.

    .. warning::
       This API is experimental.

    See :py:class:`temporalio.api.enums.v1.PendingActivityState`.
    """

    UNSPECIFIED = int(
        temporalio.api.enums.v1.PendingActivityState.PENDING_ACTIVITY_STATE_UNSPECIFIED
    )
    SCHEDULED = int(
        temporalio.api.enums.v1.PendingActivityState.PENDING_ACTIVITY_STATE_SCHEDULED
    )
    STARTED = int(
        temporalio.api.enums.v1.PendingActivityState.PENDING_ACTIVITY_STATE_STARTED
    )
    CANCEL_REQUESTED = int(
        temporalio.api.enums.v1.PendingActivityState.PENDING_ACTIVITY_STATE_CANCEL_REQUESTED
    )
    PAUSED = int(
        temporalio.api.enums.v1.PendingActivityState.PENDING_ACTIVITY_STATE_PAUSED
    )
    PAUSE_REQUESTED = int(
        temporalio.api.enums.v1.PendingActivityState.PENDING_ACTIVITY_STATE_PAUSE_REQUESTED
    )


@dataclass(frozen=True)
class ActivityExecutionCount:
    """Representation of a count from a count activities call.

    .. warning::
       This API is experimental.
    """

    count: int
    """Total count matching the filter, if any."""

    groups: Sequence[ActivityExecutionCountAggregationGroup]
    """Aggregation groups if requested."""

    @staticmethod
    def _from_raw(
        resp: temporalio.api.workflowservice.v1.CountActivityExecutionsResponse,
    ) -> ActivityExecutionCount:
        """Create from raw proto response."""
        return ActivityExecutionCount(
            count=resp.count,
            groups=[
                ActivityExecutionCountAggregationGroup._from_raw(g) for g in resp.groups
            ],
        )


@dataclass(frozen=True)
class ActivityExecutionCountAggregationGroup:
    """A single aggregation group from a count activities call.

    .. warning::
       This API is experimental.
    """

    count: int
    """Count for this group."""

    group_values: Sequence[temporalio.common.SearchAttributeValue]
    """Values that define this group."""

    @staticmethod
    def _from_raw(
        raw: temporalio.api.workflowservice.v1.CountActivityExecutionsResponse.AggregationGroup,
    ) -> ActivityExecutionCountAggregationGroup:
        return ActivityExecutionCountAggregationGroup(
            count=raw.count,
            group_values=[
                temporalio.converter._search_attributes._decode_search_attribute_value(
                    v
                )
                for v in raw.group_values
            ],
        )


@dataclass(frozen=True)
class AsyncActivityIDReference:
    """Reference to an async activity by its qualified ID."""

    workflow_id: str | None
    run_id: str | None
    activity_id: str


class AsyncActivityHandle(WithSerializationContext):
    """Handle representing an external activity for completion and heartbeat."""

    def __init__(
        self,
        client: Client,
        id_or_token: AsyncActivityIDReference | bytes,
        data_converter_override: DataConverter | None = None,
    ) -> None:
        """Create an async activity handle."""
        self._client = client
        self._id_or_token = id_or_token
        self._data_converter_override = data_converter_override

    async def heartbeat(
        self,
        *details: Any,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> None:
        """Record a heartbeat for the activity.

        Args:
            details: Details of the heartbeat.
            rpc_metadata: Headers used on the RPC call. Keys here override
                client-level RPC metadata keys.
            rpc_timeout: Optional RPC deadline to set for the RPC call.
        """
        await self._client._impl.heartbeat_async_activity(
            HeartbeatAsyncActivityInput(
                id_or_token=self._id_or_token,
                details=details,
                rpc_metadata=rpc_metadata,
                rpc_timeout=rpc_timeout,
                data_converter_override=self._data_converter_override,
            ),
        )

    async def complete(
        self,
        result: Any | None = temporalio.common._arg_unset,
        *,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> None:
        """Complete the activity.

        Args:
            result: Result of the activity if any.
            rpc_metadata: Headers used on the RPC call. Keys here override
                client-level RPC metadata keys.
            rpc_timeout: Optional RPC deadline to set for the RPC call.
        """
        await self._client._impl.complete_async_activity(
            CompleteAsyncActivityInput(
                id_or_token=self._id_or_token,
                result=result,
                rpc_metadata=rpc_metadata,
                rpc_timeout=rpc_timeout,
                data_converter_override=self._data_converter_override,
            ),
        )

    async def fail(
        self,
        error: Exception,
        *,
        last_heartbeat_details: Sequence[Any] = [],
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> None:
        """Fail the activity.

        Args:
            error: Error for the activity.
            last_heartbeat_details: Last heartbeat details for the activity.
            rpc_metadata: Headers used on the RPC call. Keys here override
                client-level RPC metadata keys.
            rpc_timeout: Optional RPC deadline to set for the RPC call.
        """
        await self._client._impl.fail_async_activity(
            FailAsyncActivityInput(
                id_or_token=self._id_or_token,
                error=error,
                last_heartbeat_details=last_heartbeat_details,
                rpc_metadata=rpc_metadata,
                rpc_timeout=rpc_timeout,
                data_converter_override=self._data_converter_override,
            ),
        )

    async def report_cancellation(
        self,
        *details: Any,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> None:
        """Report the activity as cancelled.

        Args:
            details: Cancellation details.
            rpc_metadata: Headers used on the RPC call. Keys here override
                client-level RPC metadata keys.
            rpc_timeout: Optional RPC deadline to set for the RPC call.
        """
        await self._client._impl.report_cancellation_async_activity(
            ReportCancellationAsyncActivityInput(
                id_or_token=self._id_or_token,
                details=details,
                rpc_metadata=rpc_metadata,
                rpc_timeout=rpc_timeout,
                data_converter_override=self._data_converter_override,
            ),
        )

    def with_context(self, context: SerializationContext) -> Self:
        """Create a new AsyncActivityHandle with a different serialization context.

        Payloads received by the activity will be decoded and deserialized using a data converter
        with :py:class:`ActivitySerializationContext` set as context. If you are using a custom data
        converter that makes use of this context then you can use this method to supply matching
        context data to the data converter used to serialize and encode the outbound payloads.
        """
        data_converter = self._client.data_converter.with_context(context)
        if data_converter is self._client.data_converter:
            return self
        cls = type(self)
        if cls.__init__ is not AsyncActivityHandle.__init__:
            raise TypeError(
                "If you have subclassed AsyncActivityHandle and overridden the __init__ method "
                "then you must override with_context to return an instance of your class."
            )
        return cls(
            self._client,
            self._id_or_token,
            data_converter,
        )


class ActivityHandle(Generic[ReturnType]):
    """Handle representing an activity execution not started by a workflow.

    .. warning::
       This API is experimental.
    """

    def __init__(
        self,
        client: Client,
        id: str,
        *,
        run_id: str | None = None,
        result_type: type | None = None,
    ) -> None:
        """Create activity handle."""
        self._client = client
        self._id = id
        self._run_id = run_id
        self._result_type = result_type
        self._known_outcome: (
            temporalio.api.activity.v1.ActivityExecutionOutcome | None
        ) = None

    @functools.cached_property
    def _data_converter(self) -> temporalio.converter.DataConverter:
        return self._client.data_converter.with_context(
            ActivitySerializationContext(
                namespace=self._client.namespace,
                activity_id=self._id,
                activity_type=None,
                activity_task_queue=None,
                is_local=False,
                workflow_id=None,
                workflow_type=None,
            )
        )

    @property
    def id(self) -> str:
        """ID of the activity."""
        return self._id

    @property
    def run_id(self) -> str | None:
        """Run ID of the activity."""
        return self._run_id

    async def result(
        self,
        *,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> ReturnType:
        """Wait for result of the activity.

        .. warning::
           This API is experimental.

        The result may already be known if this method has been called before,
        in which case no network call is made. Otherwise the result will be
        polled for until it is available.

        Args:
            rpc_metadata: Headers used on the RPC call. Keys here override
                client-level RPC metadata keys.
            rpc_timeout: Optional RPC deadline to set for each RPC call. Note:
                this is the timeout for each RPC call while polling, not a
                timeout for the function as a whole. If an individual RPC
                times out, it will be retried until the result is available.

        Returns:
            The result of the activity.

        Raises:
            ActivityFailureError: If the activity completed with a failure.
            RPCError: Activity result could not be fetched for some reason.
        """
        await self._poll_until_outcome(
            rpc_metadata=rpc_metadata, rpc_timeout=rpc_timeout
        )

        # Convert outcome to failure or value
        assert self._known_outcome
        if self._known_outcome.HasField("failure"):
            raise ActivityFailureError(
                cause=await self._data_converter.decode_failure(
                    self._known_outcome.failure
                ),
            )
        if not self._known_outcome.result.payloads:
            return None  # type: ignore
        type_hints = [self._result_type] if self._result_type else None
        results = await self._data_converter.decode(
            self._known_outcome.result.payloads, type_hints
        )
        if not results:
            return None  # type: ignore
        elif len(results) > 1:
            warnings.warn(f"Expected single activity result, got {len(results)}")
        return results[0]

    async def _poll_until_outcome(
        self,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> None:
        """Poll for activity result until it's available."""
        if self._known_outcome:
            return

        req = temporalio.api.workflowservice.v1.PollActivityExecutionRequest(
            namespace=self._client.namespace,
            activity_id=self._id,
            run_id=self._run_id or "",
        )

        # Continue polling as long as we have no outcome
        while True:
            try:
                res = await self._client.workflow_service.poll_activity_execution(
                    req,
                    retry=True,
                    metadata=rpc_metadata,
                    timeout=rpc_timeout,
                )
                if res.HasField("outcome"):
                    self._known_outcome = res.outcome
                    return
            except RPCError as err:
                if err.status == RPCStatusCode.DEADLINE_EXCEEDED:
                    # Deadline exceeded is expected with long polling; retry
                    continue
                elif err.status == RPCStatusCode.CANCELLED:
                    raise asyncio.CancelledError() from err
                else:
                    raise
            except asyncio.CancelledError:
                raise

    async def cancel(
        self,
        *,
        reason: str | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> None:
        """Request cancellation of the activity.

        .. warning::
           This API is experimental.

        Requesting cancellation of an activity does not automatically transition the activity to
        canceled status. If the activity is heartbeating, a :py:class:`exceptions.CancelledError`
        exception will be raised when receiving the heartbeat response; if the activity allows this
        exception to bubble out, the activity will transition to canceled status. If the activity it
        is not heartbeating, this method will have no effect on activity status.

        Args:
            reason: Reason for the cancellation. Recorded and available via describe.
            rpc_metadata: Headers used on the RPC call.
            rpc_timeout: Optional RPC deadline to set for the RPC call.
        """
        await self._client._impl.cancel_activity(
            CancelActivityInput(
                activity_id=self._id,
                activity_run_id=self._run_id,
                reason=reason,
                rpc_metadata=rpc_metadata,
                rpc_timeout=rpc_timeout,
            )
        )

    async def terminate(
        self,
        *,
        reason: str | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> None:
        """Terminate the activity execution immediately.

        .. warning::
           This API is experimental.

        Termination does not reach the worker and the activity code cannot react to it.
        A terminated activity may have a running attempt and will be requested to be
        canceled by the server when it heartbeats.

        Args:
            reason: Reason for the termination.
            rpc_metadata: Headers used on the RPC call.
            rpc_timeout: Optional RPC deadline to set for the RPC call.
        """
        await self._client._impl.terminate_activity(
            TerminateActivityInput(
                activity_id=self._id,
                activity_run_id=self._run_id,
                reason=reason,
                rpc_metadata=rpc_metadata,
                rpc_timeout=rpc_timeout,
            )
        )

    async def describe(
        self,
        *,
        include_input: bool = False,
        include_outcome: bool = False,
        include_heartbeat_details: bool = False,
        include_last_failure: bool = False,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> ActivityExecutionDescription:
        """Describe the activity execution.

        .. warning::
           This API is experimental.

        Args:
            include_input: Include activity input in the response if available.
            include_outcome: Include activity outcome in the response if available.
            include_heartbeat_details: Include heartbeat details in the response if available.
            include_last_failure: Include last failure in the response if available.
            rpc_metadata: Headers used on the RPC call.
            rpc_timeout: Optional RPC deadline to set for the RPC call.

        Returns:
            Activity execution description.
        """
        return await self._client._impl.describe_activity(
            DescribeActivityInput(
                activity_id=self._id,
                activity_run_id=self._run_id,
                include_input=include_input,
                include_outcome=include_outcome,
                include_heartbeat_details=include_heartbeat_details,
                include_last_failure=include_last_failure,
                rpc_metadata=rpc_metadata,
                rpc_timeout=rpc_timeout,
            )
        )
