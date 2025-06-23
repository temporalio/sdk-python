from __future__ import annotations

import typing
import warnings
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    Optional,
    Type,
    Union,
)

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
from ._util import is_async_callable


class WorkflowRunOperationHandler(
    nexusrpc.handler.OperationHandler[InputT, OutputT],
    Generic[InputT, OutputT, ServiceHandlerT],
):
    def __init__(
        self,
        start: Callable[
            [StartOperationContext, InputT],
            Awaitable[WorkflowOperationToken[OutputT]],
        ],
    ):
        if not is_async_callable(start):
            raise RuntimeError(
                f"{start} is not an `async def` method. "
                "WorkflowRunOperationHandler must be initialized with an "
                "`async def` start method."
            )
        self._start = start
        if start.__doc__:
            self.start.__func__.__doc__ = start.__doc__

    async def start(
        self, ctx: StartOperationContext, input: InputT
    ) -> nexusrpc.handler.StartOperationResultAsync:
        token = await self._start(ctx, input)
        return StartOperationResultAsync(token.encode())

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
