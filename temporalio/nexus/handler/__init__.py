from __future__ import annotations

import logging
import types
import typing
import warnings
from functools import wraps
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Generic,
    Optional,
    Type,
    Union,
)

import nexusrpc.handler
from nexusrpc.handler import HandlerError, HandlerErrorType
from nexusrpc.types import (
    InputT,
    OutputT,
    ServiceHandlerT,
)
from typing_extensions import Self, overload

if TYPE_CHECKING:
    from temporalio.client import (
        Client,
        WorkflowHandle,
    )

from collections.abc import Mapping
from typing import MutableMapping

from temporalio.nexus.handler._operation_context import (
    CancelOperationContext as CancelOperationContext,
)
from temporalio.nexus.handler._operation_context import (
    Context as Context,
)
from temporalio.nexus.handler._operation_context import (
    StartOperationContext as StartOperationContext,
)
from temporalio.nexus.handler._operation_context import (
    current_context,
)
from temporalio.nexus.handler._token import (
    WorkflowOperationToken as WorkflowOperationToken,
)


class LoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger, extra: Optional[Mapping[str, Any]]):
        super().__init__(logger, extra or {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        extra = dict(self.extra or {})
        if ctx := current_context.get(None):
            extra["service"] = ctx.operation_context.service
            extra["operation"] = ctx.operation_context.operation
            extra["task_queue"] = ctx.operation_context.task_queue
        kwargs["extra"] = extra | kwargs.get("extra", {})
        return msg, kwargs


logger = LoggerAdapter(logging.getLogger(__name__), None)
"""Logger that emits additional data describing the current Nexus operation."""


# TODO(nexus-preview): demonstrate obtaining Temporal client in sync operation.


def _get_workflow_run_start_method_input_and_output_type_annotations(
    start_method: Callable[
        [ServiceHandlerT, StartOperationContext, InputT],
        Awaitable[WorkflowHandle[Any, OutputT]],
    ],
) -> tuple[
    Optional[Type[InputT]],
    Optional[Type[OutputT]],
]:
    """Return operation input and output types.

    `start_method` must be a type-annotated start method that returns a
    :py:class:`WorkflowHandle`.
    """
    # TODO(nexus-preview) circular import
    from temporalio.client import WorkflowHandle

    input_type, output_type = (
        nexusrpc.handler.get_start_method_input_and_output_types_annotations(
            start_method
        )
    )
    origin_type = typing.get_origin(output_type)
    if not origin_type or not issubclass(origin_type, WorkflowHandle):
        warnings.warn(
            f"Expected return type of {start_method.__name__} to be a subclass of WorkflowHandle, "
            f"but is {output_type}"
        )
        output_type = None

    args = typing.get_args(output_type)
    if len(args) != 2:
        warnings.warn(
            f"Expected return type of {start_method.__name__} to have exactly two type parameters, "
            f"but has {len(args)}: {args}"
        )
        output_type = None
    else:
        _wf_type, output_type = args
    return input_type, output_type


# TODO(nexus-prerelease): support request_id
# See e.g. TS
# packages/nexus/src/context.ts attachRequestId
# packages/test/src/test-nexus-handler.ts ctx.requestId


async def cancel_workflow(
    ctx: CancelOperationContext,
    token: str,
    client: Optional[Client] = None,
) -> None:
    client = client or ctx.client
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
    await handle.cancel()


class WorkflowRunOperationHandler(
    nexusrpc.handler.OperationHandler[InputT, OutputT],
    Generic[InputT, OutputT, ServiceHandlerT],
):
    def __init__(
        self,
        service: ServiceHandlerT,
        start_method: Callable[
            [ServiceHandlerT, StartOperationContext, InputT],
            Awaitable[WorkflowHandle[Any, OutputT]],
        ],
        output_type: Optional[Type] = None,
    ):
        self.service = service

        @wraps(start_method)
        async def start(
            self, ctx: StartOperationContext, input: InputT
        ) -> WorkflowRunOperationResult:
            wf_handle = await start_method(service, ctx, input)
            return WorkflowRunOperationResult.from_workflow_handle(wf_handle)

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


class WorkflowRunOperationResult(nexusrpc.handler.StartOperationResultAsync):
    """
    A value returned by the start method of a :class:`WorkflowRunOperation`.

    It indicates that the operation is responding asynchronously, and contains a token
    that the handler can use to construct a :class:`~temporalio.client.WorkflowHandle` to
    interact with the workflow.
    """

    @classmethod
    def from_workflow_handle(cls, workflow_handle: WorkflowHandle) -> Self:
        token = WorkflowOperationToken.from_workflow_handle(workflow_handle).encode()
        return cls(token=token)


@overload
def workflow_run_operation_handler(
    start_method: Callable[
        [ServiceHandlerT, StartOperationContext, InputT],
        Awaitable[WorkflowHandle[Any, OutputT]],
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
            Awaitable[WorkflowHandle[Any, OutputT]],
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
            Awaitable[WorkflowHandle[Any, OutputT]],
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
                Awaitable[WorkflowHandle[Any, OutputT]],
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
            Awaitable[WorkflowHandle[Any, OutputT]],
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
