from __future__ import annotations

import typing
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, overload

import nexusrpc

import temporalio.api.common.v1
import temporalio.common
import temporalio.converter

from . import notifications
from ._operation_context import (
    TemporalStartOperationContext,
    WorkflowRunOperationContext,
)
from ._temporal_client import TemporalNexusClient, TemporalOperationResult
from ._token import WorkflowHandle
from ._util import get_operation_factory

OutputT = TypeVar("OutputT", contravariant=True)
HandlerOutputT = TypeVar("HandlerOutputT")
HandlerSourceContextT = TypeVar("HandlerSourceContextT")


class CompletionCallback(ABC, Generic[OutputT]):
    """Callback that reports the outcome of a Nexus operation.

    The type parameter is the output type that the callback accepts. Create
    callbacks with :py:func:`create_completion_callback`. Do not subclass this class.

    .. warning::
       This API is experimental and unstable.
    """

    @abstractmethod
    async def _to_proto(
        self, data_converter: temporalio.converter.DataConverter
    ) -> temporalio.api.common.v1.Callback:
        """Convert to proto representation."""
        ...


@dataclass(frozen=True, kw_only=True)
class _OnCompleteCallback(CompletionCallback[OutputT]):
    """Callback that calls an on-complete operation."""

    service: str
    """Service name."""

    operation: str
    """Operation name."""

    task_queue: str
    """Task queue of the worker that runs the operation."""

    source_context: Any
    """Value that the operation receives with the outcome.

    The client encodes it with the serialization context of the source operation.
    """

    async def _to_proto(
        self, data_converter: temporalio.converter.DataConverter
    ) -> temporalio.api.common.v1.Callback:
        """Convert to proto representation."""
        [payload] = await data_converter.encode([self.source_context])
        return temporalio.api.common.v1.Callback(
            nexus_handler=temporalio.api.common.v1.Callback.NexusHandler(
                service=self.service,
                operation=self.operation,
                task_queue_name=self.task_queue,
                source_context=payload,
            )
        )


def _describe(obj: Any) -> str:
    if isinstance(obj, nexusrpc.Operation):
        return f"operation {obj.name!r}"
    name = getattr(obj, "__qualname__", None)
    return name if isinstance(name, str) else repr(obj)


def _check_on_complete_operation(
    service: nexusrpc.ServiceDefinition,
    operation: nexusrpc.OperationDefinition[Any, Any],
) -> None:
    input_type = operation.input_type
    if (
        not (
            input_type is notifications.OnCompleteRequest
            or typing.get_origin(input_type) is notifications.OnCompleteRequest
        )
        or operation.output_type is not notifications.OnCompleteResponse
    ):
        raise ValueError(
            f"Operation {operation.name!r} of service {service.name!r} is not an "
            "on-complete operation: expected input OnCompleteRequest and output "
            f"OnCompleteResponse, got input {operation.input_type!r} and output "
            f"{operation.output_type!r}"
        )


# Service and operation names. Without a handler signature the output type is
# unknown, so the callback accepts the output of any operation.
@overload
def create_completion_callback(
    *,
    service: str,
    operation: str,
    task_queue: str,
    source_context: Any,
) -> CompletionCallback[Any]: ...


# Service definition operations. A handler that receives RawValue can accept the
# output of any operation.
@overload
def create_completion_callback(
    *,
    operation: nexusrpc.Operation[
        notifications.OnCompleteRequest[
            temporalio.common.RawValue, HandlerSourceContextT
        ],
        notifications.OnCompleteResponse,
    ],
    task_queue: str,
    source_context: HandlerSourceContextT,
) -> CompletionCallback[Any]: ...


@overload
def create_completion_callback(
    *,
    operation: nexusrpc.Operation[
        notifications.OnCompleteRequest[HandlerOutputT, HandlerSourceContextT],
        notifications.OnCompleteResponse,
    ],
    task_queue: str,
    source_context: HandlerSourceContextT,
) -> CompletionCallback[HandlerOutputT]: ...


# @sync_operation handler methods defined with async def
@overload
def create_completion_callback(
    *,
    operation: Callable[
        [
            Any,
            nexusrpc.handler.StartOperationContext,
            notifications.OnCompleteRequest[
                temporalio.common.RawValue, HandlerSourceContextT
            ],
        ],
        Awaitable[notifications.OnCompleteResponse],
    ],
    task_queue: str,
    source_context: HandlerSourceContextT,
) -> CompletionCallback[Any]: ...


@overload
def create_completion_callback(
    *,
    operation: Callable[
        [
            Any,
            nexusrpc.handler.StartOperationContext,
            notifications.OnCompleteRequest[HandlerOutputT, HandlerSourceContextT],
        ],
        Awaitable[notifications.OnCompleteResponse],
    ],
    task_queue: str,
    source_context: HandlerSourceContextT,
) -> CompletionCallback[HandlerOutputT]: ...


# @sync_operation handler methods defined with def
@overload
def create_completion_callback(
    *,
    operation: Callable[
        [
            Any,
            nexusrpc.handler.StartOperationContext,
            notifications.OnCompleteRequest[
                temporalio.common.RawValue, HandlerSourceContextT
            ],
        ],
        notifications.OnCompleteResponse,
    ],
    task_queue: str,
    source_context: HandlerSourceContextT,
) -> CompletionCallback[Any]: ...


@overload
def create_completion_callback(
    *,
    operation: Callable[
        [
            Any,
            nexusrpc.handler.StartOperationContext,
            notifications.OnCompleteRequest[HandlerOutputT, HandlerSourceContextT],
        ],
        notifications.OnCompleteResponse,
    ],
    task_queue: str,
    source_context: HandlerSourceContextT,
) -> CompletionCallback[HandlerOutputT]: ...


# @workflow_run_operation handler methods
@overload
def create_completion_callback(
    *,
    operation: Callable[
        [
            Any,
            WorkflowRunOperationContext,
            notifications.OnCompleteRequest[
                temporalio.common.RawValue, HandlerSourceContextT
            ],
        ],
        Awaitable[WorkflowHandle[notifications.OnCompleteResponse]],
    ],
    task_queue: str,
    source_context: HandlerSourceContextT,
) -> CompletionCallback[Any]: ...


@overload
def create_completion_callback(
    *,
    operation: Callable[
        [
            Any,
            WorkflowRunOperationContext,
            notifications.OnCompleteRequest[HandlerOutputT, HandlerSourceContextT],
        ],
        Awaitable[WorkflowHandle[notifications.OnCompleteResponse]],
    ],
    task_queue: str,
    source_context: HandlerSourceContextT,
) -> CompletionCallback[HandlerOutputT]: ...


# @temporal_operation handler methods
@overload
def create_completion_callback(
    *,
    operation: Callable[
        [
            Any,
            TemporalStartOperationContext,
            TemporalNexusClient,
            notifications.OnCompleteRequest[
                temporalio.common.RawValue, HandlerSourceContextT
            ],
        ],
        Awaitable[TemporalOperationResult[notifications.OnCompleteResponse]],
    ],
    task_queue: str,
    source_context: HandlerSourceContextT,
) -> CompletionCallback[Any]: ...


@overload
def create_completion_callback(
    *,
    operation: Callable[
        [
            Any,
            TemporalStartOperationContext,
            TemporalNexusClient,
            notifications.OnCompleteRequest[HandlerOutputT, HandlerSourceContextT],
        ],
        Awaitable[TemporalOperationResult[notifications.OnCompleteResponse]],
    ],
    task_queue: str,
    source_context: HandlerSourceContextT,
) -> CompletionCallback[HandlerOutputT]: ...


def create_completion_callback(
    *,
    service: str | None = None,
    operation: str | nexusrpc.Operation[Any, Any] | Callable[..., Any],
    task_queue: str,
    source_context: Any,
) -> CompletionCallback[Any]:
    """Create a callback that reports the outcome of a standalone Nexus operation.

    When the operation finishes, the server calls an on-complete operation on a worker
    for ``task_queue``. The call carries the result or the failure, and
    ``source_context``.

    An on-complete operation takes
    :py:class:`temporalio.nexus.notifications.OnCompleteRequest` and returns
    :py:class:`temporalio.nexus.notifications.OnCompleteResponse`. The server can
    deliver a callback more than once. Make the operation idempotent.

    .. warning::
       This API is experimental and unstable.

    Args:
        service: Service name. Required if ``operation`` is a name. Otherwise, leave
            it unset.
        operation: Handler method, service definition operation, or operation name.
            An operation name, or an operation that receives
            :py:class:`temporalio.common.RawValue`, accepts any output.
        task_queue: Task queue of the worker that runs the operation.
        source_context: Value that the operation receives with the outcome.

    Returns:
        Callback for the ``completion_callbacks`` argument of
        :py:meth:`temporalio.client.NexusClient.start_operation`.

    Raises:
        ValueError: If ``operation`` is not an on-complete operation of a Nexus
            service, if ``service`` and an operation name are not given together, or
            if either name is empty.
    """
    if service is not None or isinstance(operation, str):
        if not isinstance(service, str) or not isinstance(operation, str):
            raise ValueError(
                "Give a service name with an operation name. A handler method or a "
                "service definition operation identifies its own service"
            )
        if not service or not operation:
            raise ValueError("Service and operation names must not be empty")
        return _OnCompleteCallback(
            service=service,
            operation=operation,
            task_queue=task_queue,
            source_context=source_context,
        )

    op = (
        operation
        if isinstance(operation, nexusrpc.Operation)
        else get_operation_factory(operation)[1]
    )
    if op is None:
        raise ValueError(f"{_describe(operation)} is not a Nexus operation")
    if op.service is None:
        raise ValueError(
            f"{_describe(operation)} is not an operation of a Nexus service. Use an "
            "operation of a class decorated with @nexusrpc.service or "
            "@nexusrpc.handler.service_handler"
        )
    definition = op.service.operation_definitions[op.name]
    _check_on_complete_operation(op.service, definition)
    return _OnCompleteCallback(
        service=op.service.name,
        operation=definition.name,
        task_queue=task_queue,
        source_context=source_context,
    )
