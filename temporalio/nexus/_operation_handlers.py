from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from nexusrpc import (
    HandlerError,
    HandlerErrorType,
    InputT,
    OutputT,
)
from nexusrpc.handler import (
    CancelOperationContext,
    OperationHandler,
    StartOperationContext,
    StartOperationResultAsync,
    StartOperationResultSync,
)

import temporalio.nexus
from temporalio.nexus._operation_context import (
    TemporalNexusCancelOperationContext,
    TemporalNexusStartOperationContext,
    _temporal_cancel_operation_context,
)
from temporalio.nexus._temporal_client import (
    TemporalNexusClient,
    TemporalOperationResult,
    _TemporalNexusClient,
)
from temporalio.nexus._token import OperationToken, OperationTokenType, WorkflowHandle

from ._util import (
    is_async_callable,
)


class WorkflowRunOperationHandler(OperationHandler[InputT, OutputT]):
    """Operation handler for Nexus operations that start a workflow.

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
    ) -> None:
        """Initialize the workflow run operation handler."""
        if not is_async_callable(start):
            raise RuntimeError(
                f"{start} is not an `async def` method. "
                "WorkflowRunOperationHandler must be initialized with an "
                "`async def` start method."
            )
        self._start = start
        if start.__doc__:
            if start_func := getattr(self.start, "__func__", None):
                start_func.__doc__ = start.__doc__

    async def start(
        self, ctx: StartOperationContext, input: InputT
    ) -> StartOperationResultAsync:
        """Start the operation, by starting a workflow and completing asynchronously."""
        handle = await self._start(ctx, input)
        if not isinstance(handle, WorkflowHandle):
            raise RuntimeError(
                f"Expected {handle} to be a nexus.WorkflowHandle, but got {type(handle)}. "
                f"When using @workflow_run_operation you must use "
                "WorkflowRunOperationContext.start_workflow() "
                "to start a workflow that will deliver the result of the Nexus operation, "
                "and you must return the nexus.WorkflowHandle that it returns. "
                "It is not possible to use client.Client.start_workflow() and client.WorkflowHandle "
                "for this purpose."
            )
        return StartOperationResultAsync(handle.to_token())

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        """Cancel the operation, by cancelling the workflow."""
        await _cancel_workflow(token)


async def _cancel_workflow(
    token: str,
    **kwargs: Any,
) -> None:
    """Cancel a workflow that is backing a Nexus operation.

    This function is used by the Nexus worker to cancel a workflow that is backing a
    Nexus operation, i.e. started by a
    :py:func:`temporalio.nexus.workflow_run_operation`-decorated method.

    Args:
        token: The token of the workflow to cancel. kwargs: Additional keyword arguments
         to pass to the workflow cancel method.
    """
    try:
        nexus_workflow_handle = WorkflowHandle[Any].from_token(token)
    except Exception as err:
        raise HandlerError(
            "Failed to decode operation token as a workflow operation token. "
            "Canceling non-workflow operations is not supported.",
            type=HandlerErrorType.NOT_FOUND,
        ) from err

    ctx = _temporal_cancel_operation_context.get()
    try:
        client_workflow_handle = nexus_workflow_handle._to_client_workflow_handle(
            ctx.client
        )
    except Exception as err:
        raise HandlerError(
            "Failed to construct workflow handle from workflow operation token",
            type=HandlerErrorType.NOT_FOUND,
        ) from err
    await client_workflow_handle.cancel(**kwargs)


@dataclass(frozen=True)
class CancelWorkflowRunOptions:
    """Options for cancelling the workflow backing a Nexus operation.

    These options are built by :py:class:`TemporalNexusOperationHandler` and passed to
    :py:meth:`TemporalNexusOperationHandler.cancel_workflow_run`.

    .. warning::
       This API is experimental and unstable.
    """

    workflow_id: str
    """The ID of the workflow to cancel."""


class TemporalNexusOperationHandler(OperationHandler[InputT, OutputT], ABC):
    """Operation handler for Nexus operations that interact with Temporal.
    Implementations override the start_operation method.

    .. warning::
       This API is experimental and unstable.
    """

    @abstractmethod
    async def start_operation(
        self,
        ctx: TemporalNexusStartOperationContext,
        client: TemporalNexusClient,
        input: InputT,
    ) -> TemporalOperationResult[OutputT]:
        """Start the Temporal-backed Nexus operation."""
        ...

    async def start(
        self, ctx: StartOperationContext, input: InputT
    ) -> StartOperationResultSync[OutputT] | StartOperationResultAsync:
        """Start the Nexus operation using a Nexus-aware Temporal client.

        .. warning::
           This API is experimental and unstable.
        """
        nexus_client = _TemporalNexusClient()
        start_ctx = TemporalNexusStartOperationContext._from_start_operation_context(
            ctx
        )
        result = await self.start_operation(start_ctx, nexus_client, input)
        return result._to_nexus_result()

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        """Cancel a Nexus operation using its operation token.

        .. warning::
           This API is experimental and unstable.
        """
        try:
            operation_token = OperationToken.decode(token)
        except Exception as err:
            raise HandlerError(
                "Unable to decode operation token to cancel",
                type=HandlerErrorType.INTERNAL,
            ) from err

        cancel_ctx = TemporalNexusCancelOperationContext._from_cancel_operation_context(
            ctx
        )
        match operation_token.type:
            case OperationTokenType.WORKFLOW:
                options = CancelWorkflowRunOptions(
                    workflow_id=operation_token.workflow_id
                )
                await self.cancel_workflow_run(cancel_ctx, options)

    async def cancel_workflow_run(
        self,
        ctx: TemporalNexusCancelOperationContext,  # pyright: ignore[reportUnusedParameter]
        options: CancelWorkflowRunOptions,
    ) -> None:
        """Cancels the workflow backing the Nexus operation.

        .. warning::
           This API is experimental and unstable.
        """
        workflow_handle = temporalio.nexus.client().get_workflow_handle(
            options.workflow_id
        )
        await workflow_handle.cancel()
