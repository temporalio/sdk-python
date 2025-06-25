from __future__ import annotations

from typing import (
    Awaitable,
    Callable,
    Optional,
    Union,
    overload,
)

import nexusrpc
from nexusrpc.handler import (
    OperationHandler,
    StartOperationContext,
)
from nexusrpc.types import InputT, OutputT, ServiceHandlerT

from temporalio.nexus.handler._operation_handlers import (
    WorkflowRunOperationHandler,
)
from temporalio.nexus.handler._token import (
    WorkflowOperationToken,
)
from temporalio.nexus.handler._util import (
    get_workflow_run_start_method_input_and_output_type_annotations,
)


@overload
def workflow_run_operation_handler(
    start: Callable[
        [ServiceHandlerT, StartOperationContext, InputT],
        Awaitable[WorkflowOperationToken[OutputT]],
    ],
) -> Callable[
    [ServiceHandlerT, StartOperationContext, InputT],
    Awaitable[WorkflowOperationToken[OutputT]],
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
        [ServiceHandlerT, StartOperationContext, InputT],
        Awaitable[WorkflowOperationToken[OutputT]],
    ],
]: ...


def workflow_run_operation_handler(
    start: Optional[
        Callable[
            [ServiceHandlerT, StartOperationContext, InputT],
            Awaitable[WorkflowOperationToken[OutputT]],
        ]
    ] = None,
    *,
    name: Optional[str] = None,
) -> Union[
    Callable[
        [ServiceHandlerT, StartOperationContext, InputT],
        Awaitable[WorkflowOperationToken[OutputT]],
    ],
    Callable[
        [
            Callable[
                [ServiceHandlerT, StartOperationContext, InputT],
                Awaitable[WorkflowOperationToken[OutputT]],
            ]
        ],
        Callable[
            [ServiceHandlerT, StartOperationContext, InputT],
            Awaitable[WorkflowOperationToken[OutputT]],
        ],
    ],
]:
    """
    Decorator marking a method as the start method for a workflow-backed operation.
    """

    def decorator(
        start: Callable[
            [ServiceHandlerT, StartOperationContext, InputT],
            Awaitable[WorkflowOperationToken[OutputT]],
        ],
    ) -> Callable[
        [ServiceHandlerT, StartOperationContext, InputT],
        Awaitable[WorkflowOperationToken[OutputT]],
    ]:
        (
            input_type,
            output_type,
        ) = get_workflow_run_start_method_input_and_output_type_annotations(start)

        def operation_handler_factory(
            self: ServiceHandlerT,
        ) -> OperationHandler[InputT, OutputT]:
            async def _start(
                ctx: StartOperationContext, input: InputT
            ) -> WorkflowOperationToken[OutputT]:
                return await start(self, ctx, input)

            _start.__doc__ = start.__doc__
            return WorkflowRunOperationHandler(_start, input_type, output_type)

        operation_handler_factory.__nexus_operation__ = nexusrpc.Operation(
            name=name or start.__name__,
            method_name=start.__name__,
            input_type=input_type,
            output_type=output_type,
        )

        start.__nexus_operation_factory__ = operation_handler_factory
        return start

    if start is None:
        return decorator

    return decorator(start)
