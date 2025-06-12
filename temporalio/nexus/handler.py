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
from typing_extensions import Concatenate, Self, overload

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


# TODO(nexus-preview): demonstrate obtaining Temporal client in sync operation.


def _get_workflow_run_start_method_input_and_output_type_annotations(
    start_method: Callable[
        [S, nexusrpc.handler.StartOperationContext, I],
        Awaitable[WorkflowHandle[Any, O]],
    ],
) -> tuple[
    Optional[Type[I]],
    Optional[Type[O]],
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


# TODO(nexus-prerelease): Overload for string-name workflow


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
        # TODO(nexus-prerelease): are we handling empty string well elsewhere?
        task_queue = get_task_queue()
    completion_callbacks = (
        [
            # TODO(nexus-prerelease): For WorkflowRunOperation, when it handles the Nexus
            # request, it needs to copy the links to the callback in
            # StartWorkflowRequest.CompletionCallbacks and to StartWorkflowRequest.Links
            # (for backwards compatibility). PR reference in Go SDK:
            # https://github.com/temporalio/sdk-go/pull/1945
            temporalio.common.NexusCompletionCallback(
                url=ctx.callback_url, header=ctx.callback_headers
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
            _nexus_link_to_workflow_event(l) for l in ctx.inbound_links
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
        ctx.outbound_links.append(
            # TODO(nexus-prerelease): Before, WorkflowRunOperation was generating an EventReference
            # link to send back to the caller. Now, it checks if the server returned
            # the link in the StartWorkflowExecutionResponse, and if so, send the link
            # from the response to the caller. Fallback to generating the link for
            # backwards compatibility. PR reference in Go SDK:
            # https://github.com/temporalio/sdk-go/pull/1934
            link
        )
    return wf_handle


# TODO(nexus-prerelease): support request_id
# See e.g. TS
# packages/nexus/src/context.ts attachRequestId
# packages/test/src/test-nexus-handler.ts ctx.requestId


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

        @wraps(start_method)
        async def start(
            self, ctx: nexusrpc.handler.StartOperationContext, input: I
        ) -> WorkflowRunOperationResult:
            wf_handle = await start_method(service, ctx, input)
            # TODO(nexus-prerelease): Error message if user has accidentally used the normal client.start_workflow
            return WorkflowRunOperationResult.from_workflow_handle(wf_handle)

        self.start = types.MethodType(start, self)

    async def start(
        self, ctx: nexusrpc.handler.StartOperationContext, input: I
    ) -> nexusrpc.handler.StartOperationResultAsync:
        raise NotImplementedError(
            "The start method of a WorkflowRunOperation should be set "
            "dynamically in the __init__ method. (Did you forget to call super()?)"
        )

    async def cancel(
        self, ctx: nexusrpc.handler.CancelOperationContext, token: str
    ) -> None:
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
    ) -> Union[O, Awaitable[O]]:
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
        [S, nexusrpc.handler.StartOperationContext, I],
        Awaitable[WorkflowHandle[Any, O]],
    ],
) -> Callable[[S], WorkflowRunOperation[I, O, S]]: ...


@overload
def workflow_run_operation_handler(
    *,
    name: Optional[str] = None,
) -> Callable[
    [
        Callable[
            [S, nexusrpc.handler.StartOperationContext, I],
            Awaitable[WorkflowHandle[Any, O]],
        ]
    ],
    Callable[[S], WorkflowRunOperation[I, O, S]],
]: ...


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
            _get_workflow_run_start_method_input_and_output_type_annotations(
                start_method
            )
        )

        def factory(service: S) -> WorkflowRunOperation[I, O, S]:
            return WorkflowRunOperation(service, start_method, output_type=output_type)

        # TODO(nexus-prerelease): handle callable instances: __class__.__name__ as in sync_operation_handler
        method_name = getattr(start_method, "__name__", None)
        if not method_name and callable(start_method):
            method_name = start_method.__class__.__name__
        if not method_name:
            raise TypeError(
                f"Could not determine operation method name: "
                f"expected {start_method} to be a function or callable instance."
            )

        factory.__nexus_operation__ = nexusrpc.Operation._create(
            name=name,
            method_name=method_name,
            input_type=input_type,
            output_type=output_type,
        )

        return factory

    if start_method is None:
        return decorator

    return decorator(start_method)


# TODO(nexus-prerelease): confirm that it is correct not to use event_id in the following functions.
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
