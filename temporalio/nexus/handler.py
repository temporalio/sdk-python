from __future__ import annotations

import logging
import re
import types
import typing
import urllib.parse
import warnings
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    Optional,
    Sequence,
    Type,
    TypeVar,
    Union,
)

import nexusrpc.handler
from typing_extensions import Concatenate, overload

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.common
from temporalio.client import (
    Client,
    WorkflowHandle,
)
from temporalio.nexus.token import WorkflowOperationToken
from temporalio.types import (
    MethodAsyncNoParam,
    MethodAsyncSingleParam,
    MultiParamSpec,
    ParamType,
    ReturnType,
    SelfType,
)

I = TypeVar("I", contravariant=True)  # operation input
O = TypeVar("O", covariant=True)  # operation output
S = TypeVar("S")  # a service

logger = logging.getLogger(__name__)


# TODO(dan): resolve comments in proposal
# TODO(dan): comment in proposal re ABC
# TODO(dan): naming: Operation vs OperationHandler; Service interface and impl


# TODO(dan): confirm approach here: Temporal Nexus services will use this instead of
# nexusrpc.handler.Operation in order to avoid having to implement fetch_info and
# fetch_result.
class Operation(nexusrpc.handler.OperationHandler[I, O]):
    """
    Interface that must be implemented by an operation in a Temporal Nexus service.
    """

    # fetch_info and fetch_result are not currently to be implemented by Temporal Nexus services.

    async def fetch_info(
        self, token: str, ctx: nexusrpc.handler.FetchOperationInfoContext
    ) -> nexusrpc.handler.OperationInfo:
        raise NotImplementedError

    async def fetch_result(
        self, token: str, ctx: nexusrpc.handler.FetchOperationResultContext
    ) -> O:
        raise NotImplementedError


# TODO(dan): naming, visibility, make this less awkward
def get_input_and_output_types_from_workflow_run_start_method(
    start_method: Callable[
        [S, nexusrpc.handler.StartOperationContext, I],
        Awaitable[WorkflowHandle[Any, O]],
    ],
) -> tuple[
    Union[Type[I], Type[nexusrpc.handler.MISSING_TYPE]],
    Union[Type[O], Type[nexusrpc.handler.MISSING_TYPE]],
]:
    """Return operation input and output types.

    `start_method` must be a type-annotated start method that returns a
    :py:class:`WorkflowHandle`.

    The output type is the workflow output type, which is expected to be the second type
    parameter of the returned :py:class:`WorkflowHandle`.
    """
    input_type, output_type = (
        nexusrpc.handler.get_input_and_output_types_from_sync_operation_start_method(
            start_method
        )
    )
    origin_type = typing.get_origin(output_type)
    if not origin_type or not issubclass(origin_type, WorkflowHandle):
        warnings.warn(
            f"Expected return type of {start_method.__name__} to be a subclass of WorkflowHandle, "
            f"but is {output_type}"
        )
        output_type = nexusrpc.handler.MISSING_TYPE

    args = typing.get_args(output_type)
    if len(args) != 2:
        warnings.warn(
            f"Expected return type of {start_method.__name__} to have exactly two type parameters, "
            f"but has {len(args)}: {args}"
        )
        output_type = nexusrpc.handler.MISSING_TYPE
    else:
        _wf_type, output_type = args
    return input_type, output_type


# No-param overload
@overload
async def start_workflow(
    ctx: nexusrpc.handler.StartOperationContext,
    workflow: MethodAsyncNoParam[SelfType, ReturnType],
    *,
    id: str,
    client: Optional[Client] = None,
    task_queue: Optional[str] = None,
) -> WorkflowHandle[SelfType, ReturnType]: ...


# Single-param overload
@overload
async def start_workflow(
    ctx: nexusrpc.handler.StartOperationContext,
    workflow: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    id: str,
    client: Optional[Client] = None,
    task_queue: Optional[str] = None,
) -> WorkflowHandle[SelfType, ReturnType]: ...


# Multiple-params overload
@overload
async def start_workflow(
    ctx: nexusrpc.handler.StartOperationContext,
    workflow: Callable[Concatenate[SelfType, MultiParamSpec], Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    id: str,
    client: Optional[Client] = None,
    task_queue: Optional[str] = None,
) -> WorkflowHandle[SelfType, ReturnType]: ...


# TODO(dan): Overload for string-name workflow


async def start_workflow(
    ctx: nexusrpc.handler.StartOperationContext,
    workflow: Callable[..., Awaitable[Any]],
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    id: str,
    client: Optional[Client] = None,
    task_queue: Optional[str] = None,
) -> WorkflowHandle[Any, Any]:
    if client is None:
        client = get_client()
    if task_queue is None:
        # TODO(dan): are we handling empty string well elsewhere?
        task_queue = get_task_queue()
    completion_callbacks = (
        [
            # TODO(dan): For WorkflowRunOperation, when it handles the Nexus request, it
            # needs to copy the links to the callback in
            # StartWorkflowRequest.CompletionCallbacks and to StartWorkflowRequest.Links
            # (for backwards compatibility). PR reference in Go SDK:
            # https://github.com/temporalio/sdk-go/pull/1945
            temporalio.common.NexusCompletionCallback(
                url=ctx.callback_url, header=ctx.callback_header
            )
        ]
        if ctx.callback_url
        else []
    )
    # We need to pass options (completion_callbacks, links, on_conflict_options) which are
    # deliberately not exposed in any overload, hence the type error.
    wf_handle = await client.start_workflow(  # type: ignore
        workflow,
        args=temporalio.common._arg_or_args(arg, args),
        id=id,
        task_queue=task_queue,
        nexus_completion_callbacks=completion_callbacks,
        workflow_event_links=[
            _nexus_link_to_workflow_event(l) for l in ctx.caller_links
        ],
    )
    try:
        link = _workflow_event_to_nexus_link(
            _workflow_handle_to_workflow_execution_started_event_link(wf_handle)
        )
    except Exception as e:
        logger.warning(
            f"Failed to create WorkflowExecutionStarted event link for workflow {id}: {e}"
        )
    else:
        ctx.handler_links.append(
            # TODO(dan): Before, WorkflowRunOperation was generating an EventReference
            # link to send back to the caller. Now, it checks if the server returned
            # the link in the StartWorkflowExecutionResponse, and if so, send the link
            # from the response to the caller. Fallback to generating the link for
            # backwards compatibility. PR reference in Go SDK:
            # https://github.com/temporalio/sdk-go/pull/1934
            link
        )
    return wf_handle


# TODO(dan): support request_id
# The `requestId` from `nexus.StartOperationContext` (available as `ctx.requestId` in a `start` handler) is primarily used in the `sdk-typescript` repository in the following locations:
#
# 1.  **`packages/nexus/src/context.ts` (within the `startWorkflow` helper function)**:
#     *   When the `startWorkflow` helper is called (typically from a user's Nexus `start` handler or from `WorkflowRunOperation.start`), it takes `ctx: nexus.StartOperationContext` as an argument.
#     *   It then uses `ctx.requestId` to populate the `requestId` field within the `InternalWorkflowStartOptions` when preparing to start a Temporal workflow.
#     ```typescript
#     export async function startWorkflow<T extends Workflow>(
#       ctx: nexus.StartOperationContext,
#       // ...
#     ): Promise<WorkflowHandle<T>> {
#       // ...
#       const internalOptions: InternalWorkflowStartOptions = { links, requestId: ctx.requestId }; // <-- Usage here
#       // ...
#       if (workflowOptions.workflowIdConflictPolicy === 'USE_EXISTING') {
#         internalOptions.onConflictOptions = {
#           // ...
#           attachRequestId: true, // This indicates the server should attach the requestId if the workflow already exists
#         };
#       }
#       // ...
#       const handle = await client.workflow.start(workflowTypeOrFunc, startOptions);
#       // ...
#     }
#     ```
#     This means the `requestId` originating from the Nexus call is passed through to the Temporal server when a workflow is started. The Temporal server can then use this `requestId` for idempotency, ensuring that if the same `startWorkflow` command with the same `requestId` is received multiple times (e.g., due to retries), it only starts the workflow once. The `attachRequestId: true` option for `USE_EXISTING` conflict policy further ensures this `requestId` is associated even if an existing workflow is used.
#
# 2.  **`packages/test/src/test-nexus-handler.ts` (in test assertions)**:
#     *   The integration tests for Nexus handlers verify that `ctx.requestId` is correctly populated and available within the `start` handler.
#     *   For example, a test handler might assert its presence:
#         ```typescript
#         // Inside a test's OperationHandler implementation:
#         async start(ctx, input): Promise<nexus.HandlerStartOperationResult<string>> {
#           // ...
#           if (!ctx.requestId) {
#             throw new nexus.HandlerError({ message: 'expected requestId to be set', type: 'BAD_REQUEST' });
#           }
#           return { token: ctx.requestId }; // Example: using requestId as the operation token
#         },
#         ```
#     This confirms that the worker infrastructure correctly extracts the `Nexus-Request-Id` header (or equivalent from the gRPC request) and makes it available on `ctx.requestId`.
#
# In essence, the `sdk-typescript` repository uses `ctx.requestId` to bridge the Nexus concept of a request identifier to Temporal's workflow start mechanism, primarily for enabling idempotent workflow starts. The tests ensure this bridging works as expected.


# TODO(dan): Not for merge: this is not required for Temporal Nexus, but implementing in
# order to check that the design extends well to this.
async def fetch_workflow_info(
    ctx: nexusrpc.handler.FetchOperationInfoContext,
    token: str,
) -> nexusrpc.handler.OperationInfo:
    # TODO(dan)
    return nexusrpc.handler.OperationInfo(
        token=token,
        status=nexusrpc.handler.OperationState.RUNNING,
    )


# TODO(dan): Not for merge: this is not required for Temporal Nexus, but implementing temporarily in
# order to check that the design extends well to this.
async def fetch_workflow_result(
    ctx: nexusrpc.handler.FetchOperationResultContext,
    token: str,
    client: Optional[Client] = None,
) -> Any:
    # TODO(dan): type safety
    _client = client or get_client()
    handle = WorkflowOperationToken.decode(token).to_workflow_handle(_client)
    return await handle.result()


async def cancel_workflow(
    ctx: nexusrpc.handler.CancelOperationContext,
    token: str,
    client: Optional[Client] = None,
) -> None:
    _client = client or get_client()
    handle = WorkflowOperationToken.decode(token).to_workflow_handle(_client)
    await handle.cancel()


_current_context: ContextVar[_Context] = ContextVar("nexus-handler")


@dataclass
class _Context:
    client: Optional[Client]
    task_queue: Optional[str]
    service: Optional[str] = None
    operation: Optional[str] = None


def get_client() -> Client:
    context = _current_context.get(None)
    if context is None:
        raise RuntimeError("Not in Nexus handler context")
    if context.client is None:
        raise RuntimeError("Nexus handler client not set")
    return context.client


def get_task_queue() -> str:
    context = _current_context.get(None)
    if context is None:
        raise RuntimeError("Not in Nexus handler context")
    if context.task_queue is None:
        raise RuntimeError("Nexus handler task queue not set")
    return context.task_queue


class WorkflowRunOperation(nexusrpc.handler.OperationHandler[I, O], Generic[I, O, S]):
    def __init__(
        self,
        service: S,
        start_method: Callable[
            [S, nexusrpc.handler.StartOperationContext, I],
            Awaitable[WorkflowHandle[Any, O]],
        ],
        output_type: Optional[Type] = None,
    ):
        self.service = service

        # TODO(dan): get rid of first parameter?
        # TODO(dan): What is @wraps doing exactly?
        @wraps(start_method)
        async def start(
            self, ctx: nexusrpc.handler.StartOperationContext, input: I
        ) -> nexusrpc.handler.StartOperationResultAsync:
            wf_handle = await start_method(service, ctx, input)
            token = WorkflowOperationToken.from_workflow_handle(wf_handle).encode()
            return nexusrpc.handler.StartOperationResultAsync(token)

        # TODO(dan): get rid of first parameter?
        # TODO(dan): remove before merge; implementing temporarily to check that design extends well to this
        async def fetch_result(
            self, ctx: nexusrpc.handler.FetchOperationResultContext, token: str
        ) -> O:
            return await fetch_workflow_result(ctx, token)

        if output_type:
            fetch_result.__annotations__["return"] = output_type

        self.start = types.MethodType(start, self)
        self.fetch_result = types.MethodType(fetch_result, self)

    async def cancel(
        self, ctx: nexusrpc.handler.CancelOperationContext, token: str
    ) -> None:
        await cancel_workflow(ctx, token)

    # TODO(dan): remove before merge; implementing temporarily to check that design extends well to this
    async def fetch_info(
        self, ctx: nexusrpc.handler.FetchOperationInfoContext, token: str
    ) -> nexusrpc.handler.OperationInfo:
        return await fetch_workflow_info(ctx, token)


# TODO(dan): interceptor


def workflow_run_operation_handler(
    start_method: Optional[
        Callable[
            [S, nexusrpc.handler.StartOperationContext, I],
            Awaitable[WorkflowHandle[Any, O]],
        ]
    ] = None,
    *,
    name: Optional[str] = None,
) -> Union[
    Callable[[S], WorkflowRunOperation[I, O, S]],
    Callable[
        [
            Callable[
                [S, nexusrpc.handler.StartOperationContext, I],
                Awaitable[WorkflowHandle[Any, O]],
            ]
        ],
        Callable[[S], WorkflowRunOperation[I, O, S]],
    ],
]:
    def decorator(
        start_method: Callable[
            [S, nexusrpc.handler.StartOperationContext, I],
            Awaitable[WorkflowHandle[Any, O]],
        ],
    ) -> Callable[[S], WorkflowRunOperation[I, O, S]]:
        input_type, output_type = (
            get_input_and_output_types_from_workflow_run_start_method(start_method)
        )

        def factory(service: S) -> WorkflowRunOperation[I, O, S]:
            return WorkflowRunOperation(service, start_method, output_type=output_type)

        # TODO(dan): handle callable instances: __class__.__name__ as in sync_operation_handler
        nonlocal name
        name = name or getattr(start_method, "__name__", None)
        if not name:
            if cls := getattr(start_method, "__class__", None):
                name = cls.__name__
        if not name:
            raise ValueError(
                f"Could not determine operation name: expected {start_method} to be a function or callable instance"
            )
        factory.__nexus_operation__ = nexusrpc.contract.Operation._create(
            name=name,
            input_type=input_type,
            output_type=output_type,
        )

        return factory

    if start_method is None:
        return decorator

    return decorator(start_method)


# TODO(dan): confirm that it is correct not to use event_id in the following functions.
# Should the proto say explicitly that it's optional or how it behaves when it's missing?
def _workflow_handle_to_workflow_execution_started_event_link(
    handle: WorkflowHandle[Any, Any],
) -> temporalio.api.common.v1.Link.WorkflowEvent:
    if handle.first_execution_run_id is None:
        raise ValueError(
            f"Workflow handle {handle} has no first execution run ID. "
            "Cannot create WorkflowExecutionStarted event link."
        )
    return temporalio.api.common.v1.Link.WorkflowEvent(
        namespace=handle._client.namespace,
        workflow_id=handle.id,
        run_id=handle.first_execution_run_id,
        event_ref=temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
            event_type=temporalio.api.enums.v1.EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED
        ),
    )


def _workflow_event_to_nexus_link(
    workflow_event: temporalio.api.common.v1.Link.WorkflowEvent,
) -> nexusrpc.handler.Link:
    scheme = "temporal"
    namespace = urllib.parse.quote(workflow_event.namespace)
    workflow_id = urllib.parse.quote(workflow_event.workflow_id)
    run_id = urllib.parse.quote(workflow_event.run_id)
    path = f"/namespaces/{namespace}/workflows/{workflow_id}/{run_id}/history"
    query_params = urllib.parse.urlencode(
        {
            "eventType": temporalio.api.enums.v1.EventType.Name(
                workflow_event.event_ref.event_type
            ),
            "referenceType": "EventReference",
        }
    )
    return nexusrpc.handler.Link(
        url=urllib.parse.urlunparse((scheme, "", path, "", query_params, "")),
        type=workflow_event.DESCRIPTOR.full_name,
    )


def _nexus_link_to_workflow_event(
    link: nexusrpc.handler.Link,
) -> Optional[temporalio.api.common.v1.Link.WorkflowEvent]:
    path_regex = re.compile(
        r"^/namespaces/(?P<namespace>[^/]+)/workflows/(?P<workflow_id>[^/]+)/(?P<run_id>[^/]+)/history$"
    )
    url = urllib.parse.urlparse(link.url)
    match = path_regex.match(url.path)
    if not match:
        logger.warning(
            f"Invalid Nexus link: {link}. Expected path to match {path_regex.pattern}"
        )
        return None
    try:
        query_params = urllib.parse.parse_qs(url.query)
        [reference_type] = query_params.get("referenceType", [])
        if reference_type != "EventReference":
            raise ValueError(
                f"@@ Expected Nexus link URL query parameter referenceType to be EventReference but got: {reference_type}"
            )
        [event_type_name] = query_params.get("eventType", [])
        event_ref = temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
            event_type=temporalio.api.enums.v1.EventType.Value(event_type_name)
        )
    except ValueError as err:
        logger.warning(
            f"@@ Failed to parse event type from Nexus link URL query parameters: {link} ({err})"
        )
        event_ref = None

    groups = match.groupdict()
    return temporalio.api.common.v1.Link.WorkflowEvent(
        namespace=urllib.parse.unquote(groups["namespace"]),
        workflow_id=urllib.parse.unquote(groups["workflow_id"]),
        run_id=urllib.parse.unquote(groups["run_id"]),
        event_ref=event_ref,
    )
