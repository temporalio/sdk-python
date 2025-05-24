from __future__ import annotations

import base64
import json
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
from hyperlinked import hyperlinked, print
from typing_extensions import Concatenate, overload

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.common
from temporalio.client import (
    Client,
    WorkflowHandle,
)
from temporalio.types import (
    MethodAsyncNoParam,
    MethodAsyncSingleParam,
    MultiParamSpec,
    ParamType,
    ReturnType,
    SelfType,
)

O = TypeVar("O")
I = TypeVar("I")
S = TypeVar("S")

logger = logging.getLogger(__name__)


# TODO(dan): resolve comments in proposal
# TODO(dan): comment in proposal re ABC
# TODO(dan): naming: Operation vs OperationHandler; Service interface and impl


# TODO(dan): confirm approach here: Temporal Nexus services will use this instead of
# nexusrpc.handler.Operation in order to avoid having to implement fetch_info and
# fetch_result.
class Operation(nexusrpc.OperationHandler[I, O]):
    """
    Interface that must be implemented by an operation in a Temporal Nexus service.
    """

    # fetch_info and fetch_result are not currently to be implemented by Temporal Nexus services.

    async def fetch_info(
        self, token: str, options: nexusrpc.handler.FetchOperationInfoOptions
    ) -> nexusrpc.handler.OperationInfo:
        raise NotImplementedError

    async def fetch_result(
        self, token: str, options: nexusrpc.handler.FetchOperationResultOptions
    ) -> O:
        raise NotImplementedError


class StartWorkflowOperationResult(
    nexusrpc.handler.StartOperationResultAsync, Generic[O]
):
    @classmethod
    def from_workflow_handle(
        cls, workflow_handle: WorkflowHandle[Any, O]
    ) -> StartWorkflowOperationResult[O]:
        if workflow_handle.first_execution_run_id is None:
            raise ValueError(
                f"Workflow handle {workflow_handle} has no first execution run ID. "
                "Cannot create StartWorkflowOperationResult."
            )
        return cls(
            token=cls._encode_token(
                workflow_handle.id, workflow_handle.first_execution_run_id
            ),
            links=[
                # TODO(dan): Before, WorkflowRunOperation was generating an EventReference
                # link to send back to the caller. Now, it checks if the server returned
                # the link in the StartWorkflowExecutionResponse, and if so, send the link
                # from the response to the caller. Fallback to generating the link for
                # backwards compatibility. PR reference in Go SDK:
                # https://github.com/temporalio/sdk-go/pull/1934
                _workflow_event_to_nexus_link(
                    _workflow_handle_to_workflow_execution_started_event_link(
                        workflow_handle
                    )
                )
            ],
            # TODO(dan): headers
            headers={},
        )

    @staticmethod
    def _encode_token(workflow_id: str, run_id: str) -> str:
        return base64.b64encode(json.dumps([workflow_id, run_id]).encode()).decode()

    @staticmethod
    def _decode_token(token: str) -> tuple[str, str]:
        try:
            workflow_id, run_id = map(str, json.loads(base64.b64decode(token)))
        except Exception as e:
            raise ValueError(f"Invalid token: {token}") from e
        return workflow_id, run_id

    @staticmethod
    def to_workflow_handle(token: str, client: Client) -> WorkflowHandle[Any, O]:
        workflow_id, run_id = StartWorkflowOperationResult._decode_token(token)
        return client.get_workflow_handle(workflow_id, run_id=run_id)


# TODO(dan): naming, visibility, make this less awkward
def get_input_and_output_types_from_workflow_run_start_method(
    start_method: Callable[
        [S, I, nexusrpc.handler.StartOperationOptions],
        Awaitable[WorkflowHandle[Any, O]],
    ],
) -> tuple[
    Union[Type[I], Type[nexusrpc.handler.MISSING]],
    Union[Type[O], Type[nexusrpc.handler.MISSING]],
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
        output_type = nexusrpc.handler.MISSING

    args = typing.get_args(output_type)
    if len(args) != 2:
        warnings.warn(
            f"Expected return type of {start_method.__name__} to have exactly two type parameters, "
            f"but has {len(args)}: {args}"
        )
        output_type = nexusrpc.handler.MISSING
    else:
        _wf_type, output_type = args
    return input_type, output_type


# No-param overload
@overload
async def start_workflow(
    workflow: MethodAsyncNoParam[SelfType, ReturnType],
    *,
    id: str,
    options: nexusrpc.handler.StartOperationOptions,
    client: Optional[Client] = None,
    task_queue: Optional[str] = None,
) -> WorkflowHandle[SelfType, ReturnType]: ...


# Single-param overload
@overload
async def start_workflow(
    workflow: MethodAsyncSingleParam[SelfType, ParamType, ReturnType],
    arg: ParamType,
    *,
    id: str,
    options: nexusrpc.handler.StartOperationOptions,
    client: Optional[Client] = None,
    task_queue: Optional[str] = None,
) -> WorkflowHandle[SelfType, ReturnType]: ...


# Multiple-params overload
@overload
async def start_workflow(
    workflow: Callable[Concatenate[SelfType, MultiParamSpec], Awaitable[ReturnType]],
    *,
    args: Sequence[Any],
    id: str,
    options: nexusrpc.handler.StartOperationOptions,
    client: Optional[Client] = None,
    task_queue: Optional[str] = None,
) -> WorkflowHandle[SelfType, ReturnType]: ...


# TODO(dan): Overload for string-name workflow


# TODO(dan): name of AsyncWorkflowOperationResult?
async def start_workflow(
    workflow: Callable[..., Awaitable[Any]],
    arg: Any = temporalio.common._arg_unset,
    *,
    args: Sequence[Any] = [],
    id: str,
    options: nexusrpc.handler.StartOperationOptions,
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
                url=options.callback_url, header=options.callback_header
            )
        ]
        if options.callback_url
        else []
    )
    print(f"🌈 starting workflow {workflow} {id} in task queue {task_queue}")
    for link in options.links:
        print(f"🌈 link: {link}")
    # We need to pass options (completion_callbacks, links, on_conflict_options) which are
    # deliberately not exposed in any overload, hence the type error.
    return await client.start_workflow(  # type: ignore
        workflow,
        args=temporalio.common._arg_or_args(arg, args),
        id=id,
        task_queue=task_queue,
        nexus_completion_callbacks=completion_callbacks,
        workflow_event_links=[_nexus_link_to_workflow_event(l) for l in options.links],
    )


# Not for merge: this is not required for Temporal Nexus, but implementing in
# order to check that the design extends well to this.
async def fetch_workflow_info(
    operation_token: str,
    options: nexusrpc.handler.FetchOperationInfoOptions,
) -> nexusrpc.handler.OperationInfo:
    # TODO(dan)
    return nexusrpc.handler.OperationInfo(
        token=operation_token,
        status=nexusrpc.handler.OperationState.RUNNING,
    )


# Not for merge: this is not required for Temporal Nexus, but implementing temporarily in
# order to check that the design extends well to this.
async def fetch_workflow_result(
    operation_token: str,
    options: nexusrpc.handler.FetchOperationResultOptions,
    client: Optional[Client] = None,
) -> Any:
    # TODO(dan): type safety
    _client = client or get_client()
    _client = get_client()
    handle = StartWorkflowOperationResult.to_workflow_handle(operation_token, _client)
    return await handle.result()


async def cancel_workflow(
    operation_token: str,
    options: nexusrpc.handler.CancelOperationOptions,
    client: Optional[Client] = None,
) -> None:
    _client = client or get_client()
    handle = StartWorkflowOperationResult.to_workflow_handle(operation_token, _client)
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


class WorkflowRunOperation(nexusrpc.OperationHandler[I, O], Generic[I, O, S]):
    def __init__(
        self,
        service: S,
        start_method: Callable[
            [S, I, nexusrpc.handler.StartOperationOptions],
            Awaitable[WorkflowHandle[Any, O]],
        ],
        output_type: Optional[Type] = None,
    ):
        self.service = service

        # TODO(dan): get rid of first parameter?
        # TODO(dan): Is @wraps helping?
        @wraps(start_method)
        async def start(
            self, input: I, options: nexusrpc.handler.StartOperationOptions
        ) -> StartWorkflowOperationResult[O]:
            wf_handle = await start_method(service, input, options)
            return StartWorkflowOperationResult.from_workflow_handle(wf_handle)

        # TODO(dan): get rid of first parameter?
        async def fetch_result(
            self, token: str, options: nexusrpc.handler.FetchOperationResultOptions
        ) -> O:
            return await fetch_workflow_result(token, options)

        if output_type:
            fetch_result.__annotations__["return"] = output_type

        self.start = types.MethodType(start, self)
        self.fetch_result = types.MethodType(fetch_result, self)

    async def cancel(
        self, token: str, options: nexusrpc.handler.CancelOperationOptions
    ) -> None:
        await cancel_workflow(token, options)

    # TODO(dan): remove before merge; implementing temporarily to check that design extends well to this
    async def fetch_info(
        self, token: str, options: nexusrpc.handler.FetchOperationInfoOptions
    ) -> nexusrpc.handler.OperationInfo:
        return await fetch_workflow_info(token, options)


# TODO(dan): interceptor


# TODO(dan): support overriding op name
def workflow_run_operation(
    start_method: Optional[
        Callable[
            [S, I, nexusrpc.handler.StartOperationOptions],
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
                [S, I, nexusrpc.handler.StartOperationOptions],
                Awaitable[WorkflowHandle[Any, O]],
            ]
        ],
        Callable[[S], WorkflowRunOperation[I, O, S]],
    ],
]:
    def decorator(
        start_method: Callable[
            [S, I, nexusrpc.handler.StartOperationOptions],
            Awaitable[WorkflowHandle[Any, O]],
        ],
    ) -> Callable[[S], WorkflowRunOperation[I, O, S]]:
        input_type, output_type = (
            get_input_and_output_types_from_workflow_run_start_method(start_method)
        )

        def factory(service: S) -> WorkflowRunOperation[I, O, S]:
            return WorkflowRunOperation(service, start_method, output_type=output_type)

        # TODO(dan): handle callable instances: __class__.__name__ as in sync_operation
        nonlocal name
        name = name or getattr(start_method, "__name__", None)
        if not name:
            if cls := getattr(start_method, "__class__", None):
                name = cls.__name__
        if not name:
            raise ValueError(
                f"Could not determine operation name: expected {start_method} to be a function or callable instance"
            )
        factory.__nexus_operation__ = nexusrpc.handler.NexusOperationDefinition(
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
    if not (match := path_regex.match(url.path)):
        from hyperlinked import print_stack

        logger.warning(
            hyperlinked(
                f"@@ Invalid Nexus link: {link}. Expected path to match {path_regex.pattern}"
            )
        )
        print_stack()
        exit(1)
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
