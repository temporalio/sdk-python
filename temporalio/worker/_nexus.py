"""Nexus worker"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Optional,
    Sequence,
    Type,
)

import google.protobuf.json_format
import nexusrpc.handler
from nexusrpc.handler._core import SyncExecutor

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.failure.v1
import temporalio.api.nexus.v1
import temporalio.bridge.proto.nexus
import temporalio.bridge.worker
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.nexus
import temporalio.nexus.handler
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from ._interceptor import Interceptor

logger = logging.getLogger(__name__)


class _NexusWorker:
    def __init__(
        self,
        *,
        bridge_worker: Callable[[], temporalio.bridge.worker.Worker],
        client: temporalio.client.Client,
        task_queue: str,
        nexus_services: Sequence[Any],
        data_converter: temporalio.converter.DataConverter,
        interceptors: Sequence[Interceptor],
        metric_meter: temporalio.common.MetricMeter,
        executor: Optional[concurrent.futures.ThreadPoolExecutor],
    ) -> None:
        # TODO(nexus-prerelease): make it possible to query task queue of bridge worker
        # instead of passing unused task_queue into _NexusWorker,
        # _ActivityWorker, etc?
        self._bridge_worker = bridge_worker
        self._client = client
        self._task_queue = task_queue

        for service in nexus_services:
            if isinstance(service, type):
                raise TypeError(
                    f"Expected a service instance, but got a class: {service}. "
                    "Nexus services must be passed as instances, not classes."
                )
        self._handler = nexusrpc.handler.Handler(
            nexus_services,
            SyncExecutor(executor) if executor is not None else None,
        )
        self._data_converter = data_converter
        # TODO(nexus-prerelease): interceptors
        self._interceptors = interceptors
        # TODO(nexus-prerelease): metric_meter
        self._metric_meter = metric_meter
        self._running_operations: dict[bytes, asyncio.Task[Any]] = {}

    async def run(self) -> None:
        while True:
            try:
                poll_task = asyncio.create_task(self._bridge_worker().poll_nexus_task())
            except Exception as err:
                raise RuntimeError("Nexus worker failed") from err

            task = await poll_task

            if task.HasField("task"):
                task = task.task
                if task.request.HasField("start_operation"):
                    self._running_operations[task.task_token] = asyncio.create_task(
                        self._run_nexus_operation(
                            task.task_token,
                            task.request.start_operation,
                            dict(task.request.header),
                        )
                    )
                elif task.request.HasField("cancel_operation"):
                    # TODO(nexus-prerelease): report errors occurring during execution of user
                    # cancellation method
                    asyncio.create_task(
                        self._handle_cancel_operation(
                            task.request.cancel_operation, task.task_token
                        )
                    )
                else:
                    raise NotImplementedError(
                        f"Invalid Nexus task request: {task.request}"
                    )
            elif task.HasField("cancel_task"):
                task = task.cancel_task
                if _task := self._running_operations.get(task.task_token):
                    # TODO(nexus-prerelease): when do we remove the entry from _running_operations?
                    _task.cancel()
                else:
                    temporalio.nexus.logger.warning(
                        f"Received cancel_task but no running operation exists for "
                        f"task token: {task.task_token}"
                    )
            else:
                raise NotImplementedError(f"Invalid Nexus task: {task}")

    # Only call this if run() raised an error
    async def drain_poll_queue(self) -> None:
        while True:
            try:
                # Take all tasks and say we can't handle them
                task = await self._bridge_worker().poll_nexus_task()
                completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task.task.task_token
                )
                completion.error.failure.message = "Worker shutting down"
                await self._bridge_worker().complete_nexus_task(completion)
            except temporalio.bridge.worker.PollShutdownError:
                return

    async def wait_all_completed(self) -> None:
        await asyncio.gather(
            *self._running_operations.values(), return_exceptions=False
        )

    # TODO(nexus-prerelease): stack trace pruning. See sdk-typescript NexusHandler.execute
    # "Any call up to this function and including this one will be trimmed out of stack traces.""

    async def _run_nexus_operation(
        self,
        task_token: bytes,
        start_request: temporalio.api.nexus.v1.StartOperationRequest,
        header: dict[str, str],
    ) -> None:
        async def run() -> temporalio.bridge.proto.nexus.NexusTaskCompletion:
            temporalio.nexus.handler._current_context.set(
                temporalio.nexus.handler._Context(
                    client=self._client,
                    task_queue=self._task_queue,
                    service=start_request.service,
                    operation=start_request.operation,
                )
            )
            try:
                ctx = nexusrpc.handler.StartOperationContext(
                    service=start_request.service,
                    operation=start_request.operation,
                    headers=header,
                    request_id=start_request.request_id,
                    callback_url=start_request.callback,
                    inbound_links=[
                        nexusrpc.handler.Link(url=l.url, type=l.type)
                        for l in start_request.links
                    ],
                    callback_headers=dict(start_request.callback_header),
                )
                input = nexusrpc.handler.LazyValue(
                    serializer=_DummyPayloadSerializer(
                        data_converter=self._data_converter,
                        payload=start_request.payload,
                    ),
                    headers={},
                    stream=None,
                )
                try:
                    result = await self._handler.start_operation(ctx, input)
                except (
                    nexusrpc.handler.UnknownServiceError,
                    nexusrpc.handler.UnknownOperationError,
                ) as err:
                    # TODO(nexus-prerelease): error message
                    raise nexusrpc.handler.HandlerError(
                        "No matching operation handler",
                        type=nexusrpc.handler.HandlerErrorType.NOT_FOUND,
                        cause=err,
                        retryable=False,
                    ) from err

            except nexusrpc.handler.OperationError as err:
                return temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task_token,
                    completed=temporalio.api.nexus.v1.Response(
                        start_operation=temporalio.api.nexus.v1.StartOperationResponse(
                            operation_error=await self._operation_error_to_proto(err),
                        ),
                    ),
                )
            except BaseException as err:
                handler_err = _exception_to_handler_error(err)
                return temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task_token,
                    error=temporalio.api.nexus.v1.HandlerError(
                        error_type=handler_err.type.value,
                        failure=await self._exception_to_failure_proto(
                            handler_err.__cause__
                        ),
                        retry_behavior=(
                            temporalio.api.enums.v1.NexusHandlerErrorRetryBehavior.NEXUS_HANDLER_ERROR_RETRY_BEHAVIOR_RETRYABLE
                            if handler_err.retryable
                            else temporalio.api.enums.v1.NexusHandlerErrorRetryBehavior.NEXUS_HANDLER_ERROR_RETRY_BEHAVIOR_NON_RETRYABLE
                        ),
                    ),
                )
            else:
                if isinstance(result, nexusrpc.handler.StartOperationResultAsync):
                    op_resp = temporalio.api.nexus.v1.StartOperationResponse(
                        async_success=temporalio.api.nexus.v1.StartOperationResponse.Async(
                            operation_token=result.token,
                            links=[
                                temporalio.api.nexus.v1.Link(url=l.url, type=l.type)
                                for l in ctx.outbound_links
                            ],
                        )
                    )
                elif isinstance(result, nexusrpc.handler.StartOperationResultSync):
                    # TODO(nexus-prerelease): error handling here; what error type should it be?
                    [payload] = await self._data_converter.encode([result.value])
                    op_resp = temporalio.api.nexus.v1.StartOperationResponse(
                        sync_success=temporalio.api.nexus.v1.StartOperationResponse.Sync(
                            payload=payload
                        )
                    )
                else:
                    # TODO(nexus-prerelease): what should the error response be when the user has failed to wrap their return type?
                    # TODO(nexus-prerelease): unify this failure completion with the path above
                    err = TypeError(
                        "Operation start method must return either nexusrpc.handler.StartOperationResultSync "
                        "or nexusrpc.handler.StartOperationResultAsync"
                    )
                    handler_err = _exception_to_handler_error(err)
                    return temporalio.bridge.proto.nexus.NexusTaskCompletion(
                        task_token=task_token,
                        error=temporalio.api.nexus.v1.HandlerError(
                            error_type=handler_err.type.value,
                            failure=await self._exception_to_failure_proto(
                                handler_err.__cause__
                            ),
                            retry_behavior=(
                                temporalio.api.enums.v1.NexusHandlerErrorRetryBehavior.NEXUS_HANDLER_ERROR_RETRY_BEHAVIOR_RETRYABLE
                                if handler_err.retryable
                                else temporalio.api.enums.v1.NexusHandlerErrorRetryBehavior.NEXUS_HANDLER_ERROR_RETRY_BEHAVIOR_NON_RETRYABLE
                            ),
                        ),
                    )

                return temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task_token,
                    completed=temporalio.api.nexus.v1.Response(start_operation=op_resp),
                )

        try:
            completion = await run()
            await self._bridge_worker().complete_nexus_task(completion)
        except Exception:
            temporalio.nexus.logger.exception("Failed completing Nexus operation")
        finally:
            try:
                del self._running_operations[task_token]
            except KeyError:
                temporalio.nexus.logger.exception(
                    "Failed to remove completed Nexus operation"
                )

    async def _handle_cancel_operation(
        self, request: temporalio.api.nexus.v1.CancelOperationRequest, task_token: bytes
    ) -> None:
        temporalio.nexus.handler._current_context.set(
            temporalio.nexus.handler._Context(
                client=self._client,
                task_queue=self._task_queue,
                service=request.service,
                operation=request.operation,
            )
        )
        ctx = nexusrpc.handler.CancelOperationContext(
            service=request.service,
            operation=request.operation,
        )
        # TODO(nexus-prerelease): header
        try:
            await self._handler.cancel_operation(ctx, request.operation_token)
        except Exception as err:
            temporalio.nexus.logger.exception(
                "Failed to execute Nexus operation cancel method", err
            )
        # TODO(nexus-prerelease): when do we use ack_cancel?
        completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
            task_token=task_token,
            completed=temporalio.api.nexus.v1.Response(
                cancel_operation=temporalio.api.nexus.v1.CancelOperationResponse()
            ),
        )
        try:
            await self._bridge_worker().complete_nexus_task(completion)
        except Exception as err:
            temporalio.nexus.logger.exception(
                "Failed to send Nexus task completion", err
            )

    async def _exception_to_failure_proto(
        self,
        err: BaseException,
    ) -> temporalio.api.nexus.v1.Failure:
        api_failure = temporalio.api.failure.v1.Failure()
        await self._data_converter.encode_failure(err, api_failure)
        api_failure = google.protobuf.json_format.MessageToDict(api_failure)
        # TODO(nexus-prerelease): is metadata correct and playing intended role here?
        return temporalio.api.nexus.v1.Failure(
            message=api_failure.pop("message", ""),
            metadata={"type": "temporal.api.failure.v1.Failure"},
            details=json.dumps(api_failure).encode("utf-8"),
        )

    async def _operation_error_to_proto(
        self,
        err: nexusrpc.handler.OperationError,
    ) -> temporalio.api.nexus.v1.UnsuccessfulOperationError:
        cause = err.__cause__
        if cause is None:
            cause = Exception(*err.args).with_traceback(err.__traceback__)
        return temporalio.api.nexus.v1.UnsuccessfulOperationError(
            operation_state=err.state.value,
            failure=await self._exception_to_failure_proto(cause),
        )

    async def _handler_error_to_proto(
        self, err: nexusrpc.handler.HandlerError
    ) -> temporalio.api.nexus.v1.HandlerError:
        return temporalio.api.nexus.v1.HandlerError(
            error_type=err.type.value,
            failure=await self._exception_to_failure_proto(err),
            # TODO(nexus-prerelease): is there a reason to support retryable=None?
            retry_behavior=(
                temporalio.api.enums.v1.NexusHandlerErrorRetryBehavior.NEXUS_HANDLER_ERROR_RETRY_BEHAVIOR_RETRYABLE
                if err.retryable
                else temporalio.api.enums.v1.NexusHandlerErrorRetryBehavior.NEXUS_HANDLER_ERROR_RETRY_BEHAVIOR_NON_RETRYABLE
            ),
        )


@dataclass
class _DummyPayloadSerializer:
    data_converter: temporalio.converter.DataConverter
    payload: temporalio.api.common.v1.Payload

    async def serialize(self, value: Any) -> nexusrpc.handler.Content:
        raise NotImplementedError(
            "The serialize method of the Serializer is not used by handlers"
        )

    async def deserialize(
        self,
        content: nexusrpc.handler.Content,
        as_type: Optional[Type[Any]] = None,
    ) -> Any:
        try:
            [input] = await self.data_converter.decode(
                [self.payload],
                type_hints=[as_type] if as_type else None,
            )
        except Exception as err:
            raise nexusrpc.handler.HandlerError(
                "Data converter failed to decode Nexus operation input",
                type=nexusrpc.handler.HandlerErrorType.BAD_REQUEST,
                cause=err,
                retryable=False,
            ) from err
        return input


# TODO(nexus-prerelease): tests for this function
def _exception_to_handler_error(err: BaseException) -> nexusrpc.handler.HandlerError:
    # Based on sdk-typescript's convertKnownErrors:
    # https://github.com/temporalio/sdk-typescript/blob/nexus/packages/worker/src/nexus.ts
    if isinstance(err, nexusrpc.handler.HandlerError):
        return err
    elif isinstance(err, ApplicationError):
        return nexusrpc.handler.HandlerError(
            # TODO(nexus-prerelease): what should message be?
            err.message,
            type=nexusrpc.handler.HandlerErrorType.INTERNAL,
            cause=err,
            # TODO(nexus-prerelease): is there a reason to support retryable=None?
            retryable=not err.non_retryable,
        )
    elif isinstance(err, RPCError):
        if err.status == RPCStatusCode.INVALID_ARGUMENT:
            return nexusrpc.handler.HandlerError(
                err.message,
                type=nexusrpc.handler.HandlerErrorType.BAD_REQUEST,
                cause=err,
            )
        elif err.status in [
            RPCStatusCode.ALREADY_EXISTS,
            RPCStatusCode.FAILED_PRECONDITION,
            RPCStatusCode.OUT_OF_RANGE,
        ]:
            return nexusrpc.handler.HandlerError(
                err.message,
                type=nexusrpc.handler.HandlerErrorType.INTERNAL,
                cause=err,
                retryable=False,
            )
        elif err.status in [RPCStatusCode.ABORTED, RPCStatusCode.UNAVAILABLE]:
            return nexusrpc.handler.HandlerError(
                err.message,
                type=nexusrpc.handler.HandlerErrorType.UNAVAILABLE,
                cause=err,
            )
        elif err.status in [
            RPCStatusCode.CANCELLED,
            RPCStatusCode.DATA_LOSS,
            RPCStatusCode.INTERNAL,
            RPCStatusCode.UNKNOWN,
            RPCStatusCode.UNAUTHENTICATED,
            RPCStatusCode.PERMISSION_DENIED,
        ]:
            # Note that UNAUTHENTICATED and PERMISSION_DENIED have Nexus error types but
            # we convert to internal because this is not a client auth error and happens
            # when the handler fails to auth with Temporal and should be considered
            # retryable.
            return nexusrpc.handler.HandlerError(
                err.message, type=nexusrpc.handler.HandlerErrorType.INTERNAL, cause=err
            )
        elif err.status == RPCStatusCode.NOT_FOUND:
            return nexusrpc.handler.HandlerError(
                err.message, type=nexusrpc.handler.HandlerErrorType.NOT_FOUND, cause=err
            )
        elif err.status == RPCStatusCode.RESOURCE_EXHAUSTED:
            return nexusrpc.handler.HandlerError(
                err.message,
                type=nexusrpc.handler.HandlerErrorType.RESOURCE_EXHAUSTED,
                cause=err,
            )
        elif err.status == RPCStatusCode.UNIMPLEMENTED:
            return nexusrpc.handler.HandlerError(
                err.message,
                type=nexusrpc.handler.HandlerErrorType.NOT_IMPLEMENTED,
                cause=err,
            )
        elif err.status == RPCStatusCode.DEADLINE_EXCEEDED:
            return nexusrpc.handler.HandlerError(
                err.message,
                type=nexusrpc.handler.HandlerErrorType.UPSTREAM_TIMEOUT,
                cause=err,
            )
        else:
            return nexusrpc.handler.HandlerError(
                f"Unhandled RPC error status: {err.status}",
                type=nexusrpc.handler.HandlerErrorType.INTERNAL,
                cause=err,
            )
    return nexusrpc.handler.HandlerError(
        str(err), type=nexusrpc.handler.HandlerErrorType.INTERNAL, cause=err
    )
