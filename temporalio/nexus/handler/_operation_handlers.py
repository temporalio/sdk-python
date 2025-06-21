from __future__ import annotations

import types
import typing
import warnings
from functools import wraps
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Coroutine,
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
from typing_extensions import overload

from ._operation_context import TemporalNexusOperationContext
from ._token import (
    WorkflowOperationToken as WorkflowOperationToken,
)

if TYPE_CHECKING:
    from temporalio.client import (
        Client,
        WorkflowHandle,
    )


async def cancel_workflow(
    ctx: CancelOperationContext,
    token: str,
    client: Optional[Client] = None,  # noqa
    **kwargs: Any,
) -> None:
    client = client or TemporalNexusOperationContext.current().client
    try:
        decoded = WorkflowOperationToken.decode(token)
    except Exception as err:
        raise HandlerError(
            "Failed to decode workflow operation token",
            type=HandlerErrorType.NOT_FOUND,
            cause=err,
        )
    try:
        handle = decoded.to_workflow_handle(client)
    except Exception as err:
        raise HandlerError(
            "Failed to construct workflow handle from workflow operation token",
            type=HandlerErrorType.NOT_FOUND,
            cause=err,
        )
    await handle.cancel(**kwargs)


class NexusStartWorkflowRequest(Generic[OutputT]):
    """
    A request to start a workflow that will handle the Nexus operation.
    """

    def __init__(
        self, start_workflow: Coroutine[Any, Any, WorkflowHandle[Any, OutputT]], /
    ):
        if start_workflow.__qualname__ != "Client.start_workflow":
            raise ValueError(
                "NexusStartWorkflowRequest must be initialized with the coroutine "
                "object obtained by calling Client.start_workflow."
            )
        self._start_workflow = start_workflow

    async def start_workflow(self) -> WorkflowHandle[Any, OutputT]:
        # TODO(nexus-prerelease) set context such that nexus metadata is injected into request
        return await self._start_workflow

    # @classmethod
    # def from_workflow_handle(cls, workflow_handle: WorkflowHandle) -> Self:
    #     """
    #     Create a :class:`WorkflowRunOperationResult` from a :py:class:`~temporalio.client.WorkflowHandle`.
    #     """
    #     token = WorkflowOperationToken.from_workflow_handle(workflow_handle).encode()
    #     return cls(token=token)

    # def to_workflow_handle(self, client: Client) -> WorkflowHandle:
    #     """
    #     Create a :py:class:`~temporalio.client.WorkflowHandle` from a :class:`WorkflowRunOperationResult`.
    #     """
    #     workflow_operation_token = WorkflowOperationToken.decode(self.token)
    #     if workflow_operation_token.namespace != client.namespace:
    #         raise ValueError(
    #             "Cannot create a workflow handle from a workflow operation result "
    #             "with a client whose namespace is not the same as the namespace of the "
    #             "workflow operation token."
    #         )
    #     return WorkflowOperationToken.decode(self.token).to_workflow_handle(client)


class WorkflowRunOperationHandler(
    nexusrpc.handler.OperationHandler[InputT, OutputT],
    Generic[InputT, OutputT, ServiceHandlerT],
):
    def __init__(
        self,
        service: ServiceHandlerT,
        start_method: Callable[
            [ServiceHandlerT, StartOperationContext, InputT],
            Awaitable[NexusStartWorkflowRequest[OutputT]],
        ],
        output_type: Optional[Type] = None,
    ):
        self.service = service

        @wraps(start_method)
        async def start(
            self, ctx: StartOperationContext, input: InputT
        ) -> StartOperationResultAsync:
            # TODO(nexus-prerelease) It must be possible to start "normal" workflows in
            # here, and then finish up with a "nexusified" workflow.
            # TODO(nexus-prerelease) It should not be possible to construct a Nexus
            # token for a non-nexusified workflow.
            # TODO(nexus-prerelease) When `start` returns, must the workflow have been
            # started? The answer is yes, but that's yes regarding the
            # OperationHandler.start() method that is created by the decorator: it's OK
            # for the shorthand method to return a lazily evaluated start_workflow; it
            # will only ever be used in its transformed form. Note that in a
            # `OperationHandler.start` method, a user should be able to create a token
            # for a nexusified workflow and return it as a Nexus response:
            #
            # token = WorkflowOperationToken.from_workflow_handle(wf_handle).encode()
            # return StartOperationResultAsync(token)
            start_wf_request = await start_method(service, ctx, input)
            wf_handle = await start_wf_request.start_workflow()
            token = WorkflowOperationToken.from_workflow_handle(wf_handle).encode()
            return StartOperationResultAsync(token)

        self.start = types.MethodType(start, self)

    async def start(
        self, ctx: StartOperationContext, input: InputT
    ) -> nexusrpc.handler.StartOperationResultAsync:
        raise NotImplementedError(
            "The start method of a WorkflowRunOperation should be set "
            "dynamically in the __init__ method. (Did you forget to call super()?)"
        )

    async def cancel(self, ctx: CancelOperationContext, token: str) -> None:
        await cancel_workflow(ctx, token)

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
        Awaitable[NexusStartWorkflowRequest[OutputT]],
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
            Awaitable[NexusStartWorkflowRequest[OutputT]],
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
            Awaitable[NexusStartWorkflowRequest[OutputT]],
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
                Awaitable[NexusStartWorkflowRequest[OutputT]],
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
            Awaitable[NexusStartWorkflowRequest[OutputT]],
        ],
    ) -> Callable[
        [ServiceHandlerT], WorkflowRunOperationHandler[InputT, OutputT, ServiceHandlerT]
    ]:
        input_type, output_type = (
            _get_workflow_run_start_method_input_and_output_type_annotations(
                start_method
            )
        )

        def factory(
            service: ServiceHandlerT,
        ) -> WorkflowRunOperationHandler[InputT, OutputT, ServiceHandlerT]:
            return WorkflowRunOperationHandler(
                service, start_method, output_type=output_type
            )

        # TODO(nexus-prerelease): handle callable instances: __class__.__name__ as in sync_operation_handler
        method_name = getattr(start_method, "__name__", None)
        if not method_name and callable(start_method):
            method_name = start_method.__class__.__name__
        if not method_name:
            raise TypeError(
                f"Could not determine operation method name: "
                f"expected {start_method} to be a function or callable instance."
            )

        factory.__nexus_operation__ = nexusrpc.Operation(
            name=name or method_name,
            method_name=method_name,
            input_type=input_type,
            output_type=output_type,
        )

        return factory

    if start_method is None:
        return decorator

    return decorator(start_method)


def _get_workflow_run_start_method_input_and_output_type_annotations(
    start_method: Callable[
        [ServiceHandlerT, StartOperationContext, InputT],
        Awaitable[NexusStartWorkflowRequest[OutputT]],
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
    if not origin_type or not issubclass(origin_type, NexusStartWorkflowRequest):
        warnings.warn(
            f"Expected return type of {start_method.__name__} to be a subclass of NexusStartWorkflowRequest, "
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
