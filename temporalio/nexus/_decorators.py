from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import (
    Any,
    TypeAlias,
    TypeVar,
    overload,
)

import nexusrpc
from nexusrpc import InputT, OutputT
from nexusrpc.handler import (
    OperationHandler,
    StartOperationContext,
)
from typing_extensions import override

from temporalio.nexus._temporal_client import (
    TemporalCompletionClient,
    TemporalNexusClient,
    TemporalOperationResult,
)
from temporalio.types import NexusServiceType

from ._completion import Completion
from ._operation_context import (
    TemporalCompletionContext,
    TemporalStartOperationContext,
    WorkflowRunOperationContext,
)
from ._operation_handlers import (
    TemporalOperationHandler,
    WorkflowRunOperationHandler,
)
from ._token import WorkflowHandle
from ._util import (
    _CompletionHandlerDefinition,
    get_callable_name,
    get_completion_method_result_type_annotation,
    get_temporal_operation_start_method_input_and_output_type_annotations,
    get_workflow_run_start_method_input_and_output_type_annotations,
    is_async_callable,
    set_completion_handler_definition,
    set_operation_factory,
)


@overload
def workflow_run_operation(
    start: Callable[
        [NexusServiceType, WorkflowRunOperationContext, InputT],
        Awaitable[WorkflowHandle[OutputT]],
    ],
) -> Callable[
    [NexusServiceType, WorkflowRunOperationContext, InputT],
    Awaitable[WorkflowHandle[OutputT]],
]: ...


@overload
def workflow_run_operation(
    *,
    name: str | None = None,
) -> Callable[
    [
        Callable[
            [NexusServiceType, WorkflowRunOperationContext, InputT],
            Awaitable[WorkflowHandle[OutputT]],
        ]
    ],
    Callable[
        [NexusServiceType, WorkflowRunOperationContext, InputT],
        Awaitable[WorkflowHandle[OutputT]],
    ],
]: ...


def workflow_run_operation(
    start: None
    | (
        Callable[
            [NexusServiceType, WorkflowRunOperationContext, InputT],
            Awaitable[WorkflowHandle[OutputT]],
        ]
    ) = None,
    *,
    name: str | None = None,
) -> (
    Callable[
        [NexusServiceType, WorkflowRunOperationContext, InputT],
        Awaitable[WorkflowHandle[OutputT]],
    ]
    | Callable[
        [
            Callable[
                [NexusServiceType, WorkflowRunOperationContext, InputT],
                Awaitable[WorkflowHandle[OutputT]],
            ]
        ],
        Callable[
            [NexusServiceType, WorkflowRunOperationContext, InputT],
            Awaitable[WorkflowHandle[OutputT]],
        ],
    ]
):
    """Decorator marking a method as the start method for a workflow-backed operation."""

    def decorator(
        start: Callable[
            [NexusServiceType, WorkflowRunOperationContext, InputT],
            Awaitable[WorkflowHandle[OutputT]],
        ],
    ) -> Callable[
        [NexusServiceType, WorkflowRunOperationContext, InputT],
        Awaitable[WorkflowHandle[OutputT]],
    ]:
        (
            input_type,
            output_type,
        ) = get_workflow_run_start_method_input_and_output_type_annotations(start)

        def operation_handler_factory(
            self: NexusServiceType,
        ) -> OperationHandler[InputT, OutputT]:
            async def _start(
                ctx: StartOperationContext, input: InputT
            ) -> WorkflowHandle[OutputT]:
                return await start(
                    self,
                    WorkflowRunOperationContext._from_start_operation_context(ctx),
                    input,
                )

            _start.__doc__ = start.__doc__
            return WorkflowRunOperationHandler(_start)

        method_name = get_callable_name(start)
        op = nexusrpc.Operation(
            name=name or method_name,
            input_type=input_type,
            output_type=output_type,
        )
        op.method_name = method_name
        nexusrpc.set_operation(operation_handler_factory, op)

        set_operation_factory(start, operation_handler_factory)
        return start

    if start is None:
        return decorator

    return decorator(start)


CompletionT = TypeVar("CompletionT", bound=Completion[Any])

CompletionHandlerFunc: TypeAlias = Callable[
    [
        NexusServiceType,
        TemporalCompletionContext,
        TemporalCompletionClient,
        CompletionT,
    ],
    Awaitable[None],
]


@overload
def completion_operation(
    method: CompletionHandlerFunc[NexusServiceType, CompletionT],
) -> CompletionHandlerFunc[NexusServiceType, CompletionT]: ...


@overload
def completion_operation(
    *,
    name: str | None = None,
) -> Callable[
    [CompletionHandlerFunc[NexusServiceType, CompletionT]],
    CompletionHandlerFunc[NexusServiceType, CompletionT],
]: ...


def completion_operation(
    method: None | CompletionHandlerFunc[NexusServiceType, CompletionT] = None,
    *,
    name: str | None = None,
) -> (
    CompletionHandlerFunc[NexusServiceType, CompletionT]
    | Callable[
        [CompletionHandlerFunc[NexusServiceType, CompletionT]],
        CompletionHandlerFunc[NexusServiceType, CompletionT],
    ]
):
    """Decorator marking a method as a handler for operation completions.

    The method will be invoked when an operation completion addressed to this service
    and operation name is delivered to the worker. It receives a
    :py:class:`temporalio.nexus.Completion` exposing the details of the completed
    operation, along with a :py:class:`temporalio.nexus.TemporalCompletionClient` that
    associates anything started through it with the completed operation via links.

    .. warning::
       This API is experimental and unstable.
    """

    def decorator(
        method: CompletionHandlerFunc[NexusServiceType, CompletionT],
    ) -> CompletionHandlerFunc[NexusServiceType, CompletionT]:
        if not is_async_callable(method):
            raise RuntimeError(
                f"{method} is not an `async def` method. "
                "@completion_operation must decorate an `async def` method."
            )
        result_type = get_completion_method_result_type_annotation(method)
        method_name = get_callable_name(method)
        set_completion_handler_definition(
            method,
            _CompletionHandlerDefinition(
                name=name or method_name,
                method_name=method_name,
                result_type=result_type,
            ),
        )
        return method

    if method is None:
        return decorator

    return decorator(method)


TemporalOperationStartHandlerFunc: TypeAlias = Callable[
    [
        NexusServiceType,
        TemporalStartOperationContext,
        TemporalNexusClient,
        InputT,
    ],
    Awaitable[TemporalOperationResult[OutputT]],
]


@overload
def temporal_operation(
    start: TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT],
) -> TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT]: ...


@overload
def temporal_operation(
    *,
    name: str | None = None,
) -> Callable[
    [TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT]],
    TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT],
]: ...


def temporal_operation(
    start: None
    | TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT] = None,
    *,
    name: str | None = None,
) -> (
    TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT]
    | Callable[
        [TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT]],
        TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT],
    ]
):
    """Decorator marking a method as the start method for an operation that interacts with Temporal.

    .. warning::
       This API is experimental and unstable.
    """

    def decorator(
        start: TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT],
    ) -> TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT]:
        if not is_async_callable(start):
            raise RuntimeError(
                f"{start} is not an `async def` method. "
                "@temporal_operation must decorate an `async def` start method."
            )
        (
            input_type,
            output_type,
        ) = get_temporal_operation_start_method_input_and_output_type_annotations(start)

        def operation_handler_factory(
            self: NexusServiceType,
        ) -> OperationHandler[InputT, OutputT]:
            async def _start(
                ctx: TemporalStartOperationContext,
                client: TemporalNexusClient,
                input: InputT,
            ) -> TemporalOperationResult[OutputT]:
                return await start(
                    self,
                    ctx,
                    client,
                    input,
                )

            class _TemporalOperationHandler(TemporalOperationHandler):
                @override
                async def start_operation(
                    self,
                    ctx: TemporalStartOperationContext,
                    client: TemporalNexusClient,
                    input: InputT,
                ) -> TemporalOperationResult[OutputT]:
                    return await _start(ctx, client, input)

            _TemporalOperationHandler.start_operation.__doc__ = start.__doc__
            return _TemporalOperationHandler()

        method_name = get_callable_name(start)
        op = nexusrpc.Operation(
            name=name or method_name,
            input_type=input_type,
            output_type=output_type,
        )
        op.method_name = method_name
        nexusrpc.set_operation(operation_handler_factory, op)

        set_operation_factory(start, operation_handler_factory)
        return start

    if start is None:
        return decorator

    return decorator(start)
