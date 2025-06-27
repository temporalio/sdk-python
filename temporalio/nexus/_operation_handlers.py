from __future__ import annotations

from typing import (
    Any,
    Awaitable,
    Callable,
    Optional,
    Type,
)

from nexusrpc import (
    InputT,
    OperationInfo,
    OutputT,
)
from nexusrpc.handler import (
    CancelOperationContext,
    FetchOperationInfoContext,
    FetchOperationResultContext,
    HandlerError,
    HandlerErrorType,
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
)

from temporalio import client
from temporalio.nexus._operation_context import (
    _temporal_cancel_operation_context,
)
from temporalio.nexus._token import WorkflowHandle

from ._util import (
    is_async_callable,
)


class WorkflowRunOperationHandler(OperationHandler[InputT, OutputT]):
    """
    Operation handler for Nexus operations that start a workflow.

    Use this class to create an operation handler that starts a workflow by passing your
    ``start`` method to the constructor. Your ``start`` method must use
    :py:func:`temporalio.nexus.WorkflowRunOperationContext.start_workflow` to start the
    workflow.
    """

    def __init__(
        self,
        start: Callable[
            [StartOperationContext, InputT],
            Awaitable[WorkflowHandle[OutputT]],
        ],
        input_type: Optional[Type[InputT]],
        output_type: Optional[Type[OutputT]],
    ) -> None:
        if not is_async_callable(start):
            raise RuntimeError(
                f"{start} is not an `async def` method. "
                "WorkflowRunOperationHandler must be initialized with an "
                "`async def` start method."
            )
        self._start = start
        if start.__doc__:
            self.start.__func__.__doc__ = start.__doc__
        self._input_type = input_type
        self._output_type = output_type

    async def start(
        self, ctx: StartOperationContext, input: InputT
    ) -> StartOperationResultAsync:
        """
        Start the operation, by starting a workflow and completing asynchronously.
        """
        handle = await self._start(ctx, input)
        if not isinstance(handle, WorkflowHandle):
            if isinstance(handle, client.WorkflowHandle):
                raise RuntimeError(
                    f"Expected {handle} to be a nexus.WorkflowHandle, but got a client.WorkflowHandle. "
                    f"You must use WorkflowRunOperationContext.start_workflow "
                    "to start a workflow that will deliver the result of the Nexus operation, "
                    "not client.Client.start_workflow."
                )
            raise RuntimeError(
                f"Expected {handle} to be a nexus.WorkflowHandle, but got {type(handle)}. "
            )
        return StartOperationResultAsync(handle.to_token())

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        """Cancel the operation, by cancelling the workflow."""
        await cancel_operation(token)

    async def fetch_info(
        self, ctx: FetchOperationInfoContext, token: str
    ) -> OperationInfo:
        raise NotImplementedError(
            "Temporal Nexus operation handlers do not support fetching operation info."
        )

    async def fetch_result(
        self, ctx: FetchOperationResultContext, token: str
    ) -> OutputT:
        raise NotImplementedError(
            "Temporal Nexus operation handlers do not support fetching the operation result."
        )
        # An implementation is provided for future reference:
        # TODO: honor `wait` param and Request-Timeout header
        # try:
        #     nexus_handle = WorkflowHandle[OutputT].from_token(token)
        # except Exception as err:
        #     raise HandlerError(
        #         "Failed to decode operation token as workflow operation token. "
        #         "Fetching result for non-workflow operations is not supported.",
        #         type=HandlerErrorType.NOT_FOUND,
        #         cause=err,
        #     )
        # ctx = _temporal_fetch_operation_context.get()
        # try:
        #     client_handle = nexus_handle.to_workflow_handle(
        #         ctx.client, result_type=self._output_type
        #     )
        # except Exception as err:
        #     raise HandlerError(
        #         "Failed to construct workflow handle from workflow operation token",
        #         type=HandlerErrorType.NOT_FOUND,
        #         cause=err,
        #     )
        # return await client_handle.result()


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
        nexus_workflow_handle = WorkflowHandle[Any].from_token(token)
    except Exception as err:
        raise HandlerError(
            "Failed to decode operation token as workflow operation token. "
            "Canceling non-workflow operations is not supported.",
            type=HandlerErrorType.NOT_FOUND,
            cause=err,
        )

    ctx = _temporal_cancel_operation_context.get()
    try:
        client_workflow_handle = nexus_workflow_handle._to_client_workflow_handle(
            ctx.client
        )
    except Exception as err:
        raise HandlerError(
            "Failed to construct workflow handle from workflow operation token",
            type=HandlerErrorType.NOT_FOUND,
            cause=err,
        )
    await client_workflow_handle.cancel(**kwargs)
