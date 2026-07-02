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

from nexusrpc import HandlerError, HandlerErrorType, OperationError, OperationErrorState
from nexusrpc.handler import StartOperationResultAsync, StartOperationResultSync
from typing_extensions import Self

import temporalio.common
from temporalio.nexus._operation_context import (
    _start_nexus_backed_workflow_update,
    _start_nexus_backing_workflow,
    _TemporalStartOperationContext,
)
from temporalio.nexus._token import UpdateHandle
from temporalio.types import (
    MethodAsyncNoParam,
    MethodAsyncSingleParam,
    MultiParamSpec,
    ParamType,
    ReturnType,
    SelfType,
)

if TYPE_CHECKING:
    import temporalio.client
    import temporalio.workflow


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

    # Overload for no-param update
    @overload
    async def start_workflow_update(
        self,
        workflow_id: str,
        update: temporalio.workflow.UpdateMethodMultiParam[[Any], ReturnType],
        *,
        id: str | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> TemporalOperationResult[ReturnType]: ...

    # Overload for single-param update
    @overload
    async def start_workflow_update(
        self,
        workflow_id: str,
        update: temporalio.workflow.UpdateMethodMultiParam[
            [Any, ParamType], ReturnType
        ],
        arg: ParamType,
        *,
        id: str | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> TemporalOperationResult[ReturnType]: ...

    # Overload for multi-param update
    @overload
    async def start_workflow_update(
        self,
        workflow_id: str,
        update: temporalio.workflow.UpdateMethodMultiParam[MultiParamSpec, ReturnType],
        *,
        args: MultiParamSpec.args,  # type: ignore
        id: str | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> TemporalOperationResult[ReturnType]: ...

    # Overload for string-name update
    @overload
    async def start_workflow_update(
        self,
        workflow_id: str,
        update: str,
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
        id: str | None = None,
        result_type: type[ReturnType] | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
    ) -> TemporalOperationResult[ReturnType]: ...

    # draft-review: check why run_id and first_execution_run_id are not used
    # for update workflow in python sdk
    @abstractmethod
    async def start_workflow_update(
        self,
        workflow_id: str,
        update: str | Callable,
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
        id: str | None = None,
        result_type: type | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
        run_id: str | None = None,
        first_execution_run_id: str | None = None,
    ) -> TemporalOperationResult[Any]:
        """Start a workflow update as the backing asynchronous Nexus operation.

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

    async def start_workflow_update(
        self,
        workflow_id: str,
        update: str | Callable,
        arg: Any = temporalio.common._arg_unset,
        *,
        args: Sequence[Any] = [],
        id: str | None = None,
        result_type: type | None = None,
        rpc_metadata: Mapping[str, str | bytes] = {},
        rpc_timeout: timedelta | None = None,
        run_id: str | None = None,
        first_execution_run_id: str | None = None,
    ) -> TemporalOperationResult[Any]:
        """Start a workflow update as the backing asynchronous Nexus operation."""
        if not self._temporal_context.nexus_context.callback_url:
            raise HandlerError(
                "callback URL is required for a workflow update Nexus operation",
                type=HandlerErrorType.BAD_REQUEST,
            )
        with self._reserve_async_start():
            update_handle = await _start_nexus_backed_workflow_update(
                temporal_context=self._temporal_context,
                workflow_id=workflow_id,
                update=update,
                arg=arg,
                args=args,
                id=id,
                result_type=result_type,
                rpc_metadata=rpc_metadata,
                rpc_timeout=rpc_timeout,
                run_id=run_id,
                first_execution_run_id=first_execution_run_id,
            )
            # If the update has already completed, return the result synchronously
            # This is in-line with the Go implementation as well
            if update_handle._known_outcome is not None:
                try:
                    result = await update_handle.result()
                except temporalio.client.WorkflowUpdateFailedError as err:
                    raise OperationError(
                        str(err), state=OperationErrorState.FAILED
                    ) from err
                return TemporalOperationResult.sync(result)
        nexus_handle: UpdateHandle[Any] = (
            UpdateHandle._unsafe_from_client_workflow_update_handle(update_handle)
        )
        return TemporalOperationResult.async_token(nexus_handle.to_token())
