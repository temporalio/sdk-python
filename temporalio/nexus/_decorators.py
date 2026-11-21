from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import (
    Any,
    TypeAlias,
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
    TemporalNexusClient,
    TemporalOperationResult,
)
from temporalio.types import NexusServiceType, ReturnType

from ._operation_context import WorkflowRunOperationContext
from ._operation_handlers import (
    TemporalOperationHandler,
    WorkflowRunOperationHandler,
)
from ._token import WorkflowHandle
from ._util import (
    get_callable_name,
    get_temporal_operation_start_method_types,
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
    [NexusServiceType, TemporalNexusClient, InputT],
    Awaitable[TemporalOperationResult[OutputT] | OutputT],
]

_TemporalOperationMethod: TypeAlias = Callable[
    [NexusServiceType, TemporalNexusClient, InputT], Awaitable[ReturnType]
]


@overload
def temporal_operation(
    start: _TemporalOperationMethod[NexusServiceType, InputT, ReturnType],
) -> _TemporalOperationMethod[NexusServiceType, InputT, ReturnType]: ...


@overload
def temporal_operation(
    *,
    name: str | None = None,
) -> Callable[
    [_TemporalOperationMethod[NexusServiceType, InputT, ReturnType]],
    _TemporalOperationMethod[NexusServiceType, InputT, ReturnType],
]: ...


def temporal_operation(
    start: None | _TemporalOperationMethod[NexusServiceType, InputT, ReturnType] = None,
    *,
    name: str | None = None,
) -> (
    _TemporalOperationMethod[NexusServiceType, InputT, ReturnType]
    | Callable[
        [_TemporalOperationMethod[NexusServiceType, InputT, ReturnType]],
        _TemporalOperationMethod[NexusServiceType, InputT, ReturnType],
    ]
):
    """Decorator marking a method as the start method for an operation that interacts with Temporal.

    The method receives ``(self, client, input)``. Use :py:func:`temporalio.nexus.info` for invocation information.

    Declare and return the payload type to complete the Nexus operation synchronously.
    Declare :py:class:`TemporalOperationResult` with the payload type to return an
    explicit synchronous result or an asynchronous operation token. A method with an
    unspecified return type, such as ``Any`` or no annotation, must return an explicit result.

    .. warning::
       This API is experimental and unstable.
    """

    def decorator(
        start: _TemporalOperationMethod[NexusServiceType, InputT, ReturnType],
    ) -> _TemporalOperationMethod[NexusServiceType, InputT, ReturnType]:
        if not is_async_callable(start):
            raise RuntimeError(
                f"{start} is not an `async def` method. "
                "@temporal_operation must decorate an `async def` start method."
            )
        input_type, output_type, returns_wrapper = (
            get_temporal_operation_start_method_types(start)
        )

        def operation_handler_factory(
            self: NexusServiceType,
        ) -> OperationHandler[InputT, Any]:
            async def _start(
                client: TemporalNexusClient,
                input: InputT,
            ) -> ReturnType:
                return await start(
                    self,
                    client,
                    input,
                )

            class _TemporalOperationHandler(TemporalOperationHandler[Any, Any]):
                @override
                async def start_operation(
                    self,
                    client: TemporalNexusClient,
                    input: InputT,
                ) -> TemporalOperationResult[Any]:
                    result = await _start(client, input)
                    if not returns_wrapper:
                        return TemporalOperationResult.sync(result)
                    if not isinstance(result, TemporalOperationResult):
                        raise TypeError(
                            f"@temporal_operation method {start} must return a "
                            "TemporalOperationResult."
                        )
                    return result

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
