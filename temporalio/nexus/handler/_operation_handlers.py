from __future__ import annotations

import types
import typing
import warnings
from functools import wraps
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    Optional,
    Type,
    Union,
)

from typing_extensions import overload

import nexusrpc.handler
from nexusrpc.handler import (
    CancelOperationContext,
    HandlerError,
    HandlerErrorType,
    StartOperationContext,
    StartOperationResultAsync,
)
from nexusrpc.types import (
    InputT,
    OutputT,
    ServiceHandlerT,
)
from temporalio.client import Client
from temporalio.nexus.handler._operation_context import TemporalOperationContext

from ._token import (
    WorkflowOperationToken as WorkflowOperationToken,
)


class WorkflowRunOperationHandler(
    nexusrpc.handler.OperationHandler[InputT, OutputT],
    Generic[InputT, OutputT, ServiceHandlerT],
):
    def __init__(
        self,
        service: ServiceHandlerT,
        start_method: Callable[
            [ServiceHandlerT, StartOperationContext, InputT],
            Awaitable[WorkflowOperationToken[OutputT]],
        ],
    ):
        self.service = service

        @wraps(start_method)
        async def start(
            _, ctx: StartOperationContext, input: InputT
        ) -> StartOperationResultAsync:
            token = await start_method(service, ctx, input)
            return StartOperationResultAsync(token.encode())

        self.start = types.MethodType(start, self)

    async def start(
        self, ctx: StartOperationContext, input: InputT
    ) -> nexusrpc.handler.StartOperationResultAsync:
        raise NotImplementedError(
            "The start method of a WorkflowRunOperation should be set "
            "dynamically in the __init__ method. (Did you forget to call super()?)"
        )

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        tctx = TemporalOperationContext.current()
        await cancel_operation(token, tctx.client)

    def fetch_info(
        self, ctx: nexusrpc.handler.FetchOperationInfoContext, token: str
    ) -> Union[
        nexusrpc.handler.OperationInfo, Awaitable[nexusrpc.handler.OperationInfo]
    ]:
        raise NotImplementedError(
            "Temporal Nexus operation handlers do not support fetching operation info."
        )

    def fetch_result(
        self, ctx: nexusrpc.handler.FetchOperationResultContext, token: str
    ) -> Union[OutputT, Awaitable[OutputT]]:
        raise NotImplementedError(
            "Temporal Nexus operation handlers do not support fetching operation results."
        )


@overload
def workflow_run_operation_handler(
    start_method: Callable[
        [ServiceHandlerT, StartOperationContext, InputT],
        Awaitable[WorkflowOperationToken[OutputT]],
    ],
) -> Callable[
    [ServiceHandlerT], WorkflowRunOperationHandler[InputT, OutputT, ServiceHandlerT]
]: ...


@overload
def workflow_run_operation_handler(
    *,
    name: Optional[str] = None,
) -> Callable[
    [
        Callable[
            [ServiceHandlerT, StartOperationContext, InputT],
            Awaitable[WorkflowOperationToken[OutputT]],
        ]
    ],
    Callable[
        [ServiceHandlerT], WorkflowRunOperationHandler[InputT, OutputT, ServiceHandlerT]
    ],
]: ...


def workflow_run_operation_handler(
    start_method: Optional[
        Callable[
            [ServiceHandlerT, StartOperationContext, InputT],
            Awaitable[WorkflowOperationToken[OutputT]],
        ]
    ] = None,
    *,
    name: Optional[str] = None,
) -> Union[
    Callable[
        [ServiceHandlerT], WorkflowRunOperationHandler[InputT, OutputT, ServiceHandlerT]
    ],
    Callable[
        [
            Callable[
                [ServiceHandlerT, StartOperationContext, InputT],
                Awaitable[WorkflowOperationToken[OutputT]],
            ]
        ],
        Callable[
            [ServiceHandlerT],
            WorkflowRunOperationHandler[InputT, OutputT, ServiceHandlerT],
        ],
    ],
]:
    def decorator(
        start_method: Callable[
            [ServiceHandlerT, StartOperationContext, InputT],
            Awaitable[WorkflowOperationToken[OutputT]],
        ],
    ) -> Callable[
        [ServiceHandlerT], WorkflowRunOperationHandler[InputT, OutputT, ServiceHandlerT]
    ]:
        def factory(
            service: ServiceHandlerT,
        ) -> WorkflowRunOperationHandler[InputT, OutputT, ServiceHandlerT]:
            # TODO(nexus-prerelease) I was passing output_type here; why?
            return WorkflowRunOperationHandler(service, start_method)

        # TODO(nexus-prerelease): handle callable instances: __class__.__name__ as in sync_operation_handler
        method_name = getattr(start_method, "__name__", None)
        if not method_name and callable(start_method):
            method_name = start_method.__class__.__name__
        if not method_name:
            raise TypeError(
                f"Could not determine operation method name: "
                f"expected {start_method} to be a function or callable instance."
            )

        input_type, output_type = (
            _get_workflow_run_start_method_input_and_output_type_annotations(
                start_method
            )
        )

        setattr(
            factory,
            "__nexus_operation__",
            nexusrpc.Operation(
                name=name or method_name,
                method_name=method_name,
                input_type=input_type,
                output_type=output_type,
            ),
        )

        return factory

    if start_method is None:
        return decorator

    return decorator(start_method)


def _get_workflow_run_start_method_input_and_output_type_annotations(
    start_method: Callable[
        [ServiceHandlerT, StartOperationContext, InputT],
        Awaitable[WorkflowOperationToken[OutputT]],
    ],
) -> tuple[
    Optional[Type[InputT]],
    Optional[Type[OutputT]],
]:
    """Return operation input and output types.

    `start_method` must be a type-annotated start method that returns a
    :py:class:`WorkflowHandle`.
    """
    input_type, output_type = (
        nexusrpc.handler.get_start_method_input_and_output_types_annotations(
            start_method
        )
    )
    origin_type = typing.get_origin(output_type)
    if not origin_type or not issubclass(origin_type, WorkflowOperationToken):
        warnings.warn(
            f"Expected return type of {start_method.__name__} to be a subclass of WorkflowOperationToken, "
            f"but is {output_type}"
        )
        output_type = None

    args = typing.get_args(output_type)
    if len(args) != 1:
        warnings.warn(
            f"Expected return type of {start_method.__name__} to have exactly one type parameter, "
            f"but has {len(args)}: {args}"
        )
        output_type = None
    else:
        [output_type] = args
    return input_type, output_type


async def cancel_operation(
    token: str,
    client: Client,
    **kwargs: Any,
) -> None:
    """Cancel a Nexus operation.

    Args:
        token: The token of the operation to cancel.
        client: The client to use to cancel the operation.
    """
    try:
        workflow_token = WorkflowOperationToken[Any].decode(token)
    except Exception as err:
        raise HandlerError(
            "Failed to decode operation token as workflow operation token. "
            "Canceling non-workflow operations is not supported.",
            type=HandlerErrorType.NOT_FOUND,
            cause=err,
        )
    try:
        handle = workflow_token.to_workflow_handle(client)
    except Exception as err:
        raise HandlerError(
            "Failed to construct workflow handle from workflow operation token",
            type=HandlerErrorType.NOT_FOUND,
            cause=err,
        )
    await handle.cancel(**kwargs)
