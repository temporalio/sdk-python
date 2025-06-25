from __future__ import annotations

from abc import ABC, abstractmethod
from typing import (
    Any,
    Awaitable,
    Callable,
    Optional,
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
)

from temporalio.client import WorkflowHandle
from temporalio.nexus.handler._operation_context import (
    temporal_operation_context,
)
from temporalio.nexus.handler._token import WorkflowOperationToken

from ._util import (
    get_workflow_run_start_method_input_and_output_type_annotations,
    is_async_callable,
)


class WorkflowRunOperationHandler(
    nexusrpc.handler.OperationHandler[InputT, OutputT],
    ABC,
):
    """
    Operation handler for Nexus operations that start a workflow.

    Use this class to create an operation handler that starts a workflow by passing your
    ``start`` method to the constructor. Your ``start`` method must use
    :py:func:`temporalio.nexus.handler.start_workflow` to start the workflow.

    Example:

    .. code-block:: python

        @service_handler(service=MyNexusService)
        class MyNexusServiceHandler:
            @operation_handler
            def my_workflow_run_operation(
                self,
            ) -> OperationHandler[MyInput, MyOutput]:
                async def start(
                    ctx: StartOperationContext, input: MyInput
                ) -> WorkflowOperationToken[MyOutput]:
                    return await start_workflow(
                        WorkflowStartedByNexusOperation.run, input,
                        id=str(uuid.uuid4()),
                    )

                return WorkflowRunOperationHandler.from_start_workflow(start)
    """

    def __init__(
        self,
        start: Optional[
            Callable[
                [StartOperationContext, InputT],
                Awaitable[WorkflowOperationToken[OutputT]],
            ]
        ] = None,
    ) -> None:
        if start is not None:
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
        else:
            self._start = self._input_type = self._output_type = None

    @classmethod
    def from_start_workflow(
        cls,
        start_workflow: Callable[
            [StartOperationContext, InputT],
            Awaitable[WorkflowOperationToken[OutputT]],
        ],
    ) -> WorkflowRunOperationHandler[InputT, OutputT]:
        return _WorkflowRunOperationHandler(start_workflow)

    @abstractmethod
    async def start(
        self, ctx: StartOperationContext, input: InputT
    ) -> nexusrpc.handler.StartOperationResultAsync:
        """
        Start the operation, by starting a workflow and completing asynchronously.
        """
        ...

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        """Cancel the operation, by cancelling the workflow."""
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
        ctx = temporal_operation_context.get()
        try:
            handle = workflow_token.to_workflow_handle(
                ctx.client, result_type=self._output_type
            )
        except Exception as err:
            raise HandlerError(
                "Failed to construct workflow handle from workflow operation token",
                type=HandlerErrorType.NOT_FOUND,
                cause=err,
            )
        return await handle.result()


class _WorkflowRunOperationHandler(WorkflowRunOperationHandler[InputT, OutputT]):
    async def start(
        self, ctx: StartOperationContext, input: InputT
    ) -> nexusrpc.handler.StartOperationResultAsync:
        """
        Start the operation, by starting a workflow and completing asynchronously.
        """

        if self._start is None:
            raise RuntimeError(
                "Do not use _WorkflowRunOperationHandler directly. "
                "Use WorkflowRunOperationHandler.from_start_workflow instead."
            )

        token = await self._start(ctx, input)
        if not isinstance(token, WorkflowOperationToken):
            if isinstance(token, WorkflowHandle):
                raise RuntimeError(
                    f"Expected {token} to be a WorkflowOperationToken, but got a WorkflowHandle. "
                    f"You must use :py:meth:`temporalio.nexus.handler.start_workflow` "
                    "to start a workflow that will deliver the result of the Nexus operation, "
                    "not :py:meth:`temporalio.client.Client.start_workflow`."
                )
            raise RuntimeError(
                f"Expected {token} to be a WorkflowOperationToken, but got {type(token)}. "
            )
        return StartOperationResultAsync(token.encode())


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

    ctx = temporal_operation_context.get()
    try:
        handle = workflow_token.to_workflow_handle(ctx.client)
    except Exception as err:
        raise HandlerError(
            "Failed to construct workflow handle from workflow operation token",
            type=HandlerErrorType.NOT_FOUND,
            cause=err,
        )
    await handle.cancel(**kwargs)
