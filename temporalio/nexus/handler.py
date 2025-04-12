from __future__ import annotations

import base64
import json
import types
import typing
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

import nexusrpc.handler
from nexusrpc.handler import _ServiceImpl

from temporalio.client import (
    Client,
    WorkflowHandle,
)
from temporalio.common import CompletionCallback
from temporalio.types import MethodAsyncSingleParam

O = TypeVar("O")
I = TypeVar("I")
S = TypeVar("S", bound=_ServiceImpl)


class AsyncWorkflowOperationResult(nexusrpc.handler.AsyncOperationResult, Generic[O]):
    @classmethod
    def from_workflow_handle(
        cls, workflow_handle: WorkflowHandle[Any, O]
    ) -> AsyncWorkflowOperationResult[O]:
        return cls(token=cls._encode_token(workflow_handle))

    @staticmethod
    def _encode_token(workflow_handle: WorkflowHandle[Any, O]) -> str:
        return base64.b64encode(
            json.dumps([workflow_handle.id, workflow_handle.run_id or ""]).encode()
        ).decode()

    @staticmethod
    def _decode_token(token: str) -> tuple[str, str]:
        try:
            workflow_id, run_id = map(str, json.loads(base64.b64decode(token)))
        except Exception as e:
            raise ValueError(f"Invalid token: {token}") from e
        return workflow_id, run_id

    @staticmethod
    def to_workflow_handle(token: str, client: Client) -> WorkflowHandle[Any, O]:
        workflow_id, run_id = AsyncWorkflowOperationResult._decode_token(token)
        return client.get_workflow_handle(workflow_id, run_id=run_id)


async def start_workflow(
    workflow_run_method: MethodAsyncSingleParam[Any, I, O],
    arg: I,
    id: str,
    options: nexusrpc.handler.StartOperationOptions,
) -> AsyncWorkflowOperationResult[O]:
    # TODO(dan): handle client and task queue provided by user?
    _client = client()
    _task_queue = task_queue()
    completion_callbacks = (
        [CompletionCallback(url=options.callback_url, header=options.callback_header)]
        if options.callback_url
        else []
    )
    workflow_handle = await _client.start_workflow(
        workflow_run_method,
        arg,
        id=id,
        task_queue=_task_queue,
        completion_callbacks=completion_callbacks,
    )
    return AsyncWorkflowOperationResult.from_workflow_handle(workflow_handle)


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


# Not for merge: this is not required for Temporal Nexus, but implementing in
# order to check that the design extends well to this.
async def fetch_workflow_result(
    operation_token: str,
    options: nexusrpc.handler.FetchOperationResultOptions,
) -> Any:
    # TODO(dan): type safety
    # TODO(dan): handle client provided by user?
    _client = client()
    workflow_handle = AsyncWorkflowOperationResult.to_workflow_handle(
        operation_token, _client
    )
    return await workflow_handle.result()


async def cancel_workflow(
    operation_token: str,
    options: nexusrpc.handler.CancelOperationOptions,
) -> None:
    # TODO(dan): handle client provided by user?
    _client = client()
    workflow_handle = AsyncWorkflowOperationResult.to_workflow_handle(
        operation_token, _client
    )
    await workflow_handle.cancel()


_current_context: ContextVar[_Context] = ContextVar("nexus-handler")


@dataclass
class _Context:
    client: Optional[Client]
    task_queue: Optional[str]


def client() -> Client:
    context = _current_context.get(None)
    if context is None:
        raise RuntimeError("Not in Nexus handler context")
    if context.client is None:
        raise RuntimeError("Nexus handler client not set")
    return context.client


def task_queue() -> str:
    context = _current_context.get(None)
    if context is None:
        raise RuntimeError("Not in Nexus handler context")
    if context.task_queue is None:
        raise RuntimeError("Nexus handler task queue not set")
    return context.task_queue


class WorkflowOperation(nexusrpc.handler.Operation[I, O]):
    def __init__(
        self,
        service: _ServiceImpl,
        start_method: Callable[
            [_ServiceImpl, I, nexusrpc.handler.StartOperationOptions],
            Awaitable[AsyncWorkflowOperationResult[O]],
        ],
    ):
        self.service = service

        # TODO: get rid of first parameter?
        @wraps(start_method)
        async def start(
            self, input: I, options: nexusrpc.handler.StartOperationOptions
        ) -> AsyncWorkflowOperationResult[O]:
            return await start_method(service, input, options)

        # TODO: get rid of first parameter?
        async def fetch_result(
            self, token: str, options: nexusrpc.handler.FetchOperationResultOptions
        ) -> O:
            return await fetch_workflow_result(token, options)

        # TODO(dan): experimental
        [out_type] = typing.get_args(typing.get_type_hints(start_method)["return"])
        fetch_result.__annotations__["return"] = out_type

        self.start = types.MethodType(start, self)
        self.fetch_result = types.MethodType(fetch_result, self)

    async def cancel(
        self, token: str, options: nexusrpc.handler.CancelOperationOptions
    ) -> None:
        await cancel_workflow(token, options)

    async def fetch_info(
        self, token: str, options: nexusrpc.handler.FetchOperationInfoOptions
    ) -> nexusrpc.handler.OperationInfo:
        return await fetch_workflow_info(token, options)


# TODO(dan): support overriding op name
def workflow_operation(
    start_method: Callable[
        [S, I, nexusrpc.handler.StartOperationOptions],
        Awaitable[AsyncWorkflowOperationResult[O]],
    ],
) -> Callable[[S], WorkflowOperation[I, O]]:
    def factory(service: S) -> WorkflowOperation[I, O]:
        return WorkflowOperation(service, start_method)

    factory.__nexus_operation__ = nexusrpc.handler._NexusOperationDefinition(
        name=start_method.__name__
    )
    return factory
