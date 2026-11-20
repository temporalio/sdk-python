from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import (
    TypeAlias,
    cast,
    overload,
)

import nexusrpc
from nexusrpc import InputT, OutputT
from nexusrpc.handler import (
    OperationHandler,
    StartOperationContext,
)
from typing_extensions import override

import temporalio.common
from temporalio.nexus._temporal_client import (
    TemporalNexusClient,
    TemporalOperationResult,
)
from temporalio.types import NexusServiceType

from ._operation_context import (
    TemporalStartOperationContext,
    WorkflowRunOperationContext,
)
from ._operation_handlers import (
    TemporalOperationHandler,
    WorkflowRunOperationHandler,
)
from ._token import WorkflowHandle
from ._util import (
    get_callable_name,
    get_temporal_operation_start_method_input_and_output_type_annotations,
    get_workflow_run_start_method_input_and_output_type_annotations,
    is_async_callable,
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
    dynamic: bool = False,
) -> Callable[
    [TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT]],
    TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT],
]: ...


def temporal_operation(
    start: None
    | TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT] = None,
    *,
    name: str | None = None,
    dynamic: bool = False,
) -> (
    TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT]
    | Callable[
        [TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT]],
        TemporalOperationStartHandlerFunc[NexusServiceType, InputT, OutputT],
    ]
):
    """Decorator marking a method as the start method for an operation that interacts with Temporal.

    Set dynamic=True to set a Nexus operation handler as a catch-all handler,
    which will handle any unknown operation that's dispatched to that Nexus service.

    A Nexus service must only have a single dynamic handler and it must accept input
    as a temporalio.common.RawValue. Cancellation uses the same token-based handling as
    named Temporal operations.

    .. warning::
       This API is experimental and unstable.
    """
    if dynamic and name is not None:
        raise ValueError("A dynamic operation handler cannot specify name")

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
        if dynamic and input_type is not temporalio.common.RawValue:
            raise TypeError(
                "A dynamic operation handler must accept temporalio.common.RawValue"
            )

        def operation_handler_factory(
            self: NexusServiceType,
        ) -> OperationHandler[InputT, OutputT]:
            async def _start(
                ctx: TemporalStartOperationContext,
                client: TemporalNexusClient,
                input: InputT,
            ) -> TemporalOperationResult[OutputT]:
                if dynamic:
                    input = await cast(nexusrpc.LazyValue, input).consume(
                        as_type=temporalio.common.RawValue
                    )
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

        if dynamic:

            @nexusrpc.handler.operation_handler(dynamic=True)
            def dynamic_operation_handler_factory(
                self: NexusServiceType,
            ) -> OperationHandler[nexusrpc.LazyValue, OutputT]:
                return cast(
                    OperationHandler[nexusrpc.LazyValue, OutputT],
                    operation_handler_factory(self),
                )

            set_operation_factory(start, dynamic_operation_handler_factory)
        else:
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
