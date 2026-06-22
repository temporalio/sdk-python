from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    Generic,
    TypeVar,
    cast,
    overload,
)

from nexusrpc import HandlerError, HandlerErrorType
from nexusrpc.handler import StartOperationResultAsync, StartOperationResultSync
from typing_extensions import Self

import temporalio.api.common.v1
import temporalio.common
from temporalio.nexus._operation_context import (
    _start_nexus_backing_workflow,
    _TemporalStartOperationContext,
    info,
)
from temporalio.types import (
    MethodAsyncNoParam,
    MethodAsyncSingleParam,
    MultiParamSpec,
    NexusServiceType,
    ParamType,
    ReturnType,
    SelfType,
)

if TYPE_CHECKING:
    import temporalio.client


_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class TemporalOperationResult(Generic[_ResultT]):
    """Unified result: sync value or async token.

    .. warning::
       This API is experimental and unstable.
    """

    value: _ResultT | object = temporalio.common._arg_unset
    token: str | None = None

    def __post_init__(self) -> None:
        """Validate that the result represents exactly one completion mode."""
        has_value = self.value is not temporalio.common._arg_unset
        has_token = self.token is not None
        if has_value == has_token:
            raise ValueError(
                "TemporalOperationResult must have exactly one of value or token set."
            )
        if has_token and (not isinstance(self.token, str) or not self.token):
            raise ValueError(
                "TemporalOperationResult token must be a non-empty string."
            )

    @classmethod
    def sync(cls, value: _ResultT) -> Self:
        """Create a result that completes the Nexus operation synchronously."""
        return cls(value=value)

    @classmethod
    def async_token(cls, token: str) -> Self:
        """Create a result that completes the Nexus operation asynchronously."""
        return cls(token=token)

    def _to_nexus_result(
        self,
    ) -> StartOperationResultSync[_ResultT] | StartOperationResultAsync:
        if self.token is not None:
            return StartOperationResultAsync(self.token)
        elif self.value is not temporalio.common._arg_unset:
            return StartOperationResultSync(cast(_ResultT, self.value))
        else:
            raise RuntimeError(
                "Invalid TemporalOperationResult. Neither token nor value are set."
            )


class TemporalNexusClient(ABC):
    """Nexus-aware wrapper around a Temporal Client.

    .. warning::
       This API is experimental and unstable.
    """

    @property
    @abstractmethod
    def client(self) -> temporalio.client.Client:
        """The underlying Temporal Client

        .. warning::
           This API is experimental and unstable.
        """
        ...

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
    ) -> TemporalOperationResult[ReturnType]: ...

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
    ) -> TemporalOperationResult[ReturnType]: ...

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
    ) -> TemporalOperationResult[ReturnType]: ...

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
    ) -> TemporalOperationResult[ReturnType]: ...

    @abstractmethod
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
    ) -> TemporalOperationResult[ReturnType]:
        """Start a workflow as the backing asynchronous Nexus operation.

        .. warning::
           This API is experimental and unstable.
        """
        ...


class _TemporalNexusClient(TemporalNexusClient):  # pyright: ignore[reportUnusedClass]
    """Nexus-aware wrapper around a Temporal Client.

    .. warning::
       This API is experimental and unstable.
    """

    def __init__(self) -> None:
        """Initialize the client wrapper from the active Nexus operation context."""
        self._temporal_context = _TemporalStartOperationContext.get()
        self._started_async = False

    @property
    def client(self) -> temporalio.client.Client:
        """Return the Temporal client for the active Nexus operation."""
        return self._temporal_context.client

    @contextmanager
    def _reserve_async_start(self) -> Iterator[None]:
        if self._started_async:
            raise HandlerError(
                "Only one async operation can be started per operation handler invocation. Use TemporalNexusClient.client for additional workflow interactions",
                type=HandlerErrorType.BAD_REQUEST,
            )

        # Reserve the started flag before sending to prevent concurrent starts
        self._started_async = True
        try:
            yield
        except BaseException:
            self._started_async = False
            raise

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
    ) -> TemporalOperationResult[ReturnType]:
        """Start a workflow as the backing asynchronous Nexus operation."""
        with self._reserve_async_start():
            wf_handle = await _start_nexus_backing_workflow(
                temporal_context=self._temporal_context,
                workflow=workflow,
                arg=arg,
                args=args,
                id=id,
                task_queue=task_queue,
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
            )

        return TemporalOperationResult.async_token(wf_handle.to_token())


class TemporalCompletionClient(ABC):
    """Nexus-aware wrapper around a Temporal Client for use in completion handlers.

    Workflows started via this client have the completion's links attached, associating
    them with the completed operation.

    .. warning::
       This API is experimental and unstable.
    """

    @property
    @abstractmethod
    def client(self) -> temporalio.client.Client:
        """The underlying Temporal Client

        .. warning::
           This API is experimental and unstable.
        """
        ...

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
    ) -> temporalio.client.WorkflowHandle[SelfType, ReturnType]: ...

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
    ) -> temporalio.client.WorkflowHandle[SelfType, ReturnType]: ...

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
    ) -> temporalio.client.WorkflowHandle[SelfType, ReturnType]: ...

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
    ) -> temporalio.client.WorkflowHandle[Any, ReturnType]: ...

    @abstractmethod
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
    ) -> temporalio.client.WorkflowHandle[Any, ReturnType]:
        """Start a workflow, attaching the completion's links to it.

        See :py:meth:`temporalio.client.Client.start_workflow` for all arguments.

        .. warning::
           This API is experimental and unstable.
        """
        ...

    @abstractmethod
    async def start_activity(
        self,
        activity: (
            str | Callable[..., Awaitable[ReturnType]] | Callable[..., ReturnType]
        ),
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
        id: str,
        task_queue: str,
        result_type: type | None = None,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        id_reuse_policy: temporalio.common.ActivityIDReusePolicy = temporalio.common.ActivityIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy: temporalio.common.ActivityIDConflictPolicy = temporalio.common.ActivityIDConflictPolicy.FAIL,
        retry_policy: temporalio.common.RetryPolicy | None = None,
        search_attributes: temporalio.common.TypedSearchAttributes | None = None,
        summary: str | None = None,
        priority: temporalio.common.Priority = temporalio.common.Priority.default,
        start_delay: timedelta | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> temporalio.client.ActivityHandle[ReturnType]:
        """Start an activity.

        See :py:meth:`temporalio.client.Client.start_activity` for all arguments.

        .. warning::
           This API is experimental and unstable. The completion's links are not yet
           attached to the started activity.
        """
        ...

    @abstractmethod
    def create_nexus_client(
        self,
        service: type[NexusServiceType] | str,
        endpoint: str,
    ) -> temporalio.client.NexusClient[NexusServiceType]:
        """Create a client for starting standalone Nexus operations.

        See :py:meth:`temporalio.client.Client.create_nexus_client` for all arguments.

        .. warning::
           This API is experimental and unstable. The completion's links are not yet
           attached to started Nexus operations.
        """
        ...


class _TemporalCompletionClient(TemporalCompletionClient):  # pyright: ignore[reportUnusedClass]
    """Nexus-aware wrapper around a Temporal Client for use in completion handlers."""

    def __init__(
        self,
        client: temporalio.client.Client,
        links: Sequence[temporalio.api.common.v1.Link],
    ) -> None:
        """Initialize the client wrapper with the completion's links."""
        self._client = client
        self._links = list(links)

    @property
    def client(self) -> temporalio.client.Client:
        """Return the underlying Temporal client."""
        return self._client

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
    ) -> temporalio.client.WorkflowHandle[Any, ReturnType]:
        """Start a workflow, attaching the completion's links to it."""
        return await self._client.start_workflow(  # type: ignore
            workflow=workflow,
            arg=arg,
            args=args,
            id=id,
            task_queue=task_queue or info().task_queue,
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
            links=self._links,
        )

    async def start_activity(
        self,
        activity: (
            str | Callable[..., Awaitable[ReturnType]] | Callable[..., ReturnType]
        ),
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
        id: str,
        task_queue: str,
        result_type: type | None = None,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        id_reuse_policy: temporalio.common.ActivityIDReusePolicy = temporalio.common.ActivityIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy: temporalio.common.ActivityIDConflictPolicy = temporalio.common.ActivityIDConflictPolicy.FAIL,
        retry_policy: temporalio.common.RetryPolicy | None = None,
        search_attributes: temporalio.common.TypedSearchAttributes | None = None,
        summary: str | None = None,
        priority: temporalio.common.Priority = temporalio.common.Priority.default,
        start_delay: timedelta | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> temporalio.client.ActivityHandle[ReturnType]:
        """Start an activity."""
        # TODO(nexus-preview): attach the completion's links to the started activity.
        # StartActivityExecutionRequest has a links field but the client does not expose
        # it yet.
        return await self._client.start_activity(
            activity=activity,  # type: ignore
            arg=arg,
            args=args,
            id=id,
            task_queue=task_queue,
            result_type=result_type,
            schedule_to_close_timeout=schedule_to_close_timeout,
            schedule_to_start_timeout=schedule_to_start_timeout,
            start_to_close_timeout=start_to_close_timeout,
            heartbeat_timeout=heartbeat_timeout,
            id_reuse_policy=id_reuse_policy,
            id_conflict_policy=id_conflict_policy,
            retry_policy=retry_policy,
            search_attributes=search_attributes,
            summary=summary,
            priority=priority,
            start_delay=start_delay,
            rpc_metadata=rpc_metadata,
            rpc_timeout=rpc_timeout,
        )

    def create_nexus_client(
        self,
        service: type[NexusServiceType] | str,
        endpoint: str,
    ) -> temporalio.client.NexusClient[NexusServiceType]:
        """Create a client for starting standalone Nexus operations."""
        # TODO(nexus-preview): attach the completion's links to started Nexus
        # operations. StartNexusOperationExecutionRequest does not have a links field
        # yet.
        return self._client.create_nexus_client(service=service, endpoint=endpoint)
