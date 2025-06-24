from __future__ import annotations

from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
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

from temporalio.nexus.handler._operation_context import TemporalOperationContext
from temporalio.nexus.handler._token import WorkflowOperationToken

from ._util import (
    get_workflow_run_start_method_input_and_output_type_annotations,
    is_async_callable,
)


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
        self._input_type, self._output_type = (
            get_workflow_run_start_method_input_and_output_type_annotations(start)
        )

    async def start(
        self, ctx: StartOperationContext, input: InputT
    ) -> nexusrpc.handler.StartOperationResultAsync:
        token = await self._start(ctx, input)
        return StartOperationResultAsync(token.encode())

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        await cancel_operation(token)

    async def fetch_info(
        self, ctx: nexusrpc.handler.FetchOperationInfoContext, token: str
    ) -> nexusrpc.handler.OperationInfo:
        raise NotImplementedError(
            "Temporal Nexus operation handlers do not support fetching operation info."
        )

    async def fetch_result(
        self, ctx: nexusrpc.handler.FetchOperationResultContext, token: str
    ) -> OutputT:
        raise NotImplementedError(
            "Temporal Nexus operation handlers do not support fetching the operation result."
        )
        # An implementation is provided for future reference:
        try:
            workflow_token = WorkflowOperationToken[OutputT].decode(token)
        except Exception as err:
            raise HandlerError(
                "Failed to decode operation token as workflow operation token. "
                "Fetching result for non-workflow operations is not supported.",
                type=HandlerErrorType.NOT_FOUND,
                cause=err,
            )
        tctx = TemporalOperationContext.get()
        try:
            handle = workflow_token.to_workflow_handle(
                tctx.client, result_type=self._output_type
            )
        except Exception as err:
            raise HandlerError(
                "Failed to construct workflow handle from workflow operation token",
                type=HandlerErrorType.NOT_FOUND,
                cause=err,
            )
        return await handle.result()


async def cancel_operation(
    token: str,
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

    tctx = TemporalOperationContext.get()
    try:
        handle = workflow_token.to_workflow_handle(tctx.client)
    except Exception as err:
        raise HandlerError(
            "Failed to construct workflow handle from workflow operation token",
            type=HandlerErrorType.NOT_FOUND,
            cause=err,
        )
    await handle.cancel(**kwargs)
