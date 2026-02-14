"""Nexus worker"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import (
    Any,
    NoReturn,
    ParamSpec,
    TypeVar,
)

import nexusrpc.handler
from nexusrpc.handler import CancelOperationContext, StartOperationContext

import temporalio.api.nexus.v1
import temporalio.bridge.proto.nexus
import temporalio.bridge.worker
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.nexus
from temporalio.bridge.worker import PollShutdownError
from temporalio.exceptions import (
    ApplicationError,
    CancelledError,
    FailureError,
    WorkflowAlreadyStartedError,
)
from temporalio.nexus import Info, logger
from temporalio.service import RPCError, RPCStatusCode

from ._interceptor import (
    Interceptor,
)
from ._nexus_handler import _TemporalNexusHandler

_TEMPORAL_FAILURE_PROTO_TYPE = "temporal.api.failure.v1.Failure"


@dataclass
class _RunningNexusTask:
    task: asyncio.Task[Any]
    cancellation: _NexusTaskCancellation

    def cancel(self, reason: str):
        self.cancellation.cancel(reason)
        self.task.cancel()


class _NexusWorker:  # type:ignore[reportUnusedClass]
    def __init__(
        self,
        *,
        bridge_worker: Callable[[], temporalio.bridge.worker.Worker],
        client: temporalio.client.Client,
        task_queue: str,
        service_handlers: Sequence[Any],
        data_converter: temporalio.converter.DataConverter,
        interceptors: Sequence[Interceptor],
        metric_meter: temporalio.common.MetricMeter,
        executor: concurrent.futures.ThreadPoolExecutor | None,
    ) -> None:
        self._bridge_worker = bridge_worker
        self._client = client
        self._task_queue = task_queue

        self._metric_meter = metric_meter

        # If an executor is provided, we wrap the executor with one that will
        # copy the contextvars.Context to the thread on submit
        handler_executor = _ContextPropagatingExecutor(executor) if executor else None
        self._handler = _TemporalNexusHandler(
            service_handlers, interceptors, data_converter, handler_executor
        )

        self._data_converter = data_converter

        self._running_tasks: dict[bytes, _RunningNexusTask] = {}
        self._fail_worker_exception_queue: asyncio.Queue[Exception] = asyncio.Queue()
        self._worker_shutdown_event: temporalio.common._CompositeEvent | None = None

    async def run(
        self,
        payload_error_limits: temporalio.converter._ServerPayloadErrorLimits | None,
    ) -> None:
        """Continually poll for Nexus tasks and dispatch to handlers."""
        self._data_converter = self._data_converter._with_payload_error_limits(
            payload_error_limits
        )

        async def raise_from_exception_queue() -> NoReturn:
            raise await self._fail_worker_exception_queue.get()

        exception_task = asyncio.create_task(raise_from_exception_queue())

        while True:
            try:
                poll_task = asyncio.create_task(self._bridge_worker().poll_nexus_task())
                await asyncio.wait(
                    [poll_task, exception_task], return_when=asyncio.FIRST_COMPLETED
                )
                if exception_task.done():
                    poll_task.cancel()
                    await exception_task
                nexus_task = await poll_task

                if nexus_task.HasField("task"):
                    task = nexus_task.task
                    if task.request.HasField("start_operation"):
                        task_cancellation = _NexusTaskCancellation()
                        start_op_task = asyncio.create_task(
                            self._handle_start_operation_task(
                                task.task_token,
                                task.request.start_operation,
                                dict(task.request.header),
                                task_cancellation,
                            )
                        )
                        self._running_tasks[task.task_token] = _RunningNexusTask(
                            start_op_task, task_cancellation
                        )
                    elif task.request.HasField("cancel_operation"):
                        task_cancellation = _NexusTaskCancellation()
                        cancel_op_task = asyncio.create_task(
                            self._handle_cancel_operation_task(
                                task.task_token,
                                task.request.cancel_operation,
                                dict(task.request.header),
                                task_cancellation,
                            )
                        )
                        self._running_tasks[task.task_token] = _RunningNexusTask(
                            cancel_op_task, task_cancellation
                        )
                    else:
                        raise NotImplementedError(
                            f"Invalid Nexus task request: {task.request}"
                        )
                elif nexus_task.HasField("cancel_task"):
                    if running_task := self._running_tasks.get(
                        nexus_task.cancel_task.task_token
                    ):
                        reason = (
                            temporalio.bridge.proto.nexus.NexusTaskCancelReason.Name(
                                nexus_task.cancel_task.reason
                            )
                        )
                        running_task.cancel(reason)
                    else:
                        logger.debug(
                            f"Received cancel_task but no running task exists for "
                            f"task token: {nexus_task.cancel_task.task_token.decode()}"
                        )
                else:
                    raise NotImplementedError(f"Invalid Nexus task: {nexus_task}")

            except PollShutdownError:
                exception_task.cancel()
                return

            except Exception as err:
                raise RuntimeError("Nexus worker failed") from err

    def notify_shutdown(self) -> None:
        if self._worker_shutdown_event:
            self._worker_shutdown_event.set()

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
            except PollShutdownError:
                return

    # Only call this after run()/drain_poll_queue() have returned. This will not
    # raise an exception.
    async def wait_all_completed(self) -> None:
        running_tasks = [
            running_task.task for running_task in self._running_tasks.values()
        ]
        await asyncio.gather(*running_tasks, return_exceptions=True)

    # TODO(nexus-preview): stack trace pruning. See sdk-typescript NexusHandler.execute
    # "Any call up to this function and including this one will be trimmed out of stack traces.""

    async def _handle_cancel_operation_task(
        self,
        task_token: bytes,
        request: temporalio.api.nexus.v1.CancelOperationRequest,
        headers: Mapping[str, str],
        task_cancellation: nexusrpc.handler.OperationTaskCancellation,
    ) -> None:
        """Handle a cancel operation task.

        Attempt to execute the user cancel_operation method. Handle errors and send the
        task completion.
        """
        # Create the worker shutdown event if not created
        if not self._worker_shutdown_event:
            self._worker_shutdown_event = temporalio.common._CompositeEvent(
                thread_event=threading.Event(), async_event=asyncio.Event()
            )
        # TODO(nexus-prerelease): headers
        ctx = CancelOperationContext(
            service=request.service,
            operation=request.operation,
            headers=headers,
            task_cancellation=task_cancellation,
        )
        temporalio.nexus._operation_context._TemporalCancelOperationContext(
            info=lambda: Info(task_queue=self._task_queue),
            nexus_context=ctx,
            client=self._client,
            _runtime_metric_meter=self._metric_meter,
            _worker_shutdown_event=self._worker_shutdown_event,
        ).set()
        try:
            try:
                await self._handler.cancel_operation(ctx, request.operation_token)
            except asyncio.CancelledError:
                completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task_token,
                    ack_cancel=task_cancellation.is_cancelled(),
                )
            except BaseException as err:
                logger.warning("Failed to execute Nexus cancel operation method")
                handler_error = _exception_to_handler_error(err)
                completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task_token,
                )
                await self._data_converter.encode_failure(
                    handler_error, completion.failure
                )
            else:
                completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task_token,
                    completed=temporalio.api.nexus.v1.Response(
                        cancel_operation=temporalio.api.nexus.v1.CancelOperationResponse()
                    ),
                )

            await self._bridge_worker().complete_nexus_task(completion)
        except Exception:
            logger.exception("Failed to send Nexus task completion")
        finally:
            try:
                del self._running_tasks[task_token]
            except KeyError:
                logger.exception(
                    "Failed to remove task for completed Nexus cancel operation"
                )

    async def _handle_start_operation_task(
        self,
        task_token: bytes,
        start_request: temporalio.api.nexus.v1.StartOperationRequest,
        headers: Mapping[str, str],
        task_cancellation: nexusrpc.handler.OperationTaskCancellation,
    ) -> None:
        """Handle a start operation task.

        Attempt to execute the user start_operation method and invoke the data converter
        on the result. Handle errors and send the task completion.
        """
        try:
            try:
                start_response = await self._start_operation(
                    start_request, headers, task_cancellation
                )
            except asyncio.CancelledError:
                completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task_token,
                    ack_cancel=task_cancellation.is_cancelled(),
                )
            except BaseException as err:
                logger.warning("Failed to execute Nexus start operation method")
                completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task_token,
                )
                handler_error = _exception_to_handler_error(err)
                await self._data_converter.encode_failure(
                    handler_error, completion.failure
                )

                if isinstance(err, concurrent.futures.BrokenExecutor):
                    self._fail_worker_exception_queue.put_nowait(err)
            else:
                completion = temporalio.bridge.proto.nexus.NexusTaskCompletion(
                    task_token=task_token,
                    completed=temporalio.api.nexus.v1.Response(
                        start_operation=start_response
                    ),
                )

            await self._bridge_worker().complete_nexus_task(completion)
        except Exception:
            logger.exception("Failed to send Nexus task completion")
        finally:
            try:
                del self._running_tasks[task_token]
            except KeyError:
                logger.exception(
                    "Failed to remove task for completed Nexus start operation"
                )

    async def _start_operation(
        self,
        start_request: temporalio.api.nexus.v1.StartOperationRequest,
        headers: Mapping[str, str],
        cancellation: nexusrpc.handler.OperationTaskCancellation,
    ) -> temporalio.api.nexus.v1.StartOperationResponse:
        """Invoke the Nexus handler's start_operation method and construct the StartOperationResponse.

        OperationError is handled by this function, since it results in a StartOperationResponse.

        All other exceptions are handled by a caller of this function.
        """
        # Create the worker shutdown event if not created
        if not self._worker_shutdown_event:
            self._worker_shutdown_event = temporalio.common._CompositeEvent(
                thread_event=threading.Event(), async_event=asyncio.Event()
            )
        ctx = StartOperationContext(
            service=start_request.service,
            operation=start_request.operation,
            headers=headers,
            request_id=start_request.request_id,
            callback_url=start_request.callback,
            inbound_links=[
                nexusrpc.Link(url=link.url, type=link.type)
                for link in start_request.links
            ],
            callback_headers=dict(start_request.callback_header),
            task_cancellation=cancellation,
        )
        temporalio.nexus._operation_context._TemporalStartOperationContext(
            nexus_context=ctx,
            client=self._client,
            info=lambda: Info(task_queue=self._task_queue),
            _runtime_metric_meter=self._metric_meter,
            _worker_shutdown_event=self._worker_shutdown_event,
        ).set()
        try:
            result = await self._handler.start_operation(ctx, start_request.payload)
            links = [
                temporalio.api.nexus.v1.Link(url=link.url, type=link.type)
                for link in ctx.outbound_links
            ]
            if isinstance(result, nexusrpc.handler.StartOperationResultAsync):
                return temporalio.api.nexus.v1.StartOperationResponse(
                    async_success=temporalio.api.nexus.v1.StartOperationResponse.Async(
                        operation_token=result.token,
                        links=links,
                    )
                )
            elif isinstance(result, nexusrpc.handler.StartOperationResultSync):
                [payload] = await self._data_converter.encode([result.value])
                return temporalio.api.nexus.v1.StartOperationResponse(
                    sync_success=temporalio.api.nexus.v1.StartOperationResponse.Sync(
                        payload=payload,
                        links=links,
                    )
                )
            else:
                raise _exception_to_handler_error(
                    TypeError(
                        "Operation start method must return either "
                        "nexusrpc.handler.StartOperationResultSync or "
                        "nexusrpc.handler.StartOperationResultAsync."
                    )
                )
        except nexusrpc.OperationError as err:
            # Convert OperationError to a Temporal failure
            try:
                match err.state:
                    case nexusrpc.OperationErrorState.CANCELED:
                        raise CancelledError(err.message) from err.__cause__
                    case nexusrpc.OperationErrorState.FAILED:
                        raise ApplicationError(
                            message=err.message,
                            type="OperationError",
                            non_retryable=True,
                        ) from err.__cause__
            except FailureError as new_err:
                response = temporalio.api.nexus.v1.StartOperationResponse()
                await self._data_converter.encode_failure(new_err, response.failure)
                return response


def _exception_to_handler_error(err: BaseException) -> nexusrpc.HandlerError:
    # Based on sdk-typescript's convertKnownErrors:
    # https://github.com/temporalio/sdk-typescript/blob/nexus/packages/worker/src/nexus.ts
    if isinstance(err, nexusrpc.HandlerError):
        return err
    elif isinstance(err, ApplicationError):
        handler_err = nexusrpc.HandlerError(
            message="Handler failed with non-retryable application error",
            type=nexusrpc.HandlerErrorType.INTERNAL,
            retryable_override=not err.non_retryable,
        )
    elif isinstance(err, WorkflowAlreadyStartedError):
        handler_err = nexusrpc.HandlerError(
            err.message,
            type=nexusrpc.HandlerErrorType.INTERNAL,
            retryable_override=False,
        )
    elif isinstance(err, RPCError):
        if err.status == RPCStatusCode.INVALID_ARGUMENT:
            handler_err = nexusrpc.HandlerError(
                err.message,
                type=nexusrpc.HandlerErrorType.BAD_REQUEST,
            )
        elif err.status in [
            RPCStatusCode.ALREADY_EXISTS,
            RPCStatusCode.FAILED_PRECONDITION,
            RPCStatusCode.OUT_OF_RANGE,
        ]:
            handler_err = nexusrpc.HandlerError(
                err.message,
                type=nexusrpc.HandlerErrorType.INTERNAL,
                retryable_override=False,
            )
        elif err.status in [RPCStatusCode.ABORTED, RPCStatusCode.UNAVAILABLE]:
            handler_err = nexusrpc.HandlerError(
                err.message,
                type=nexusrpc.HandlerErrorType.UNAVAILABLE,
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
            handler_err = nexusrpc.HandlerError(
                err.message, type=nexusrpc.HandlerErrorType.INTERNAL
            )
        elif err.status == RPCStatusCode.NOT_FOUND:
            handler_err = nexusrpc.HandlerError(
                err.message, type=nexusrpc.HandlerErrorType.NOT_FOUND
            )
        elif err.status == RPCStatusCode.RESOURCE_EXHAUSTED:
            handler_err = nexusrpc.HandlerError(
                err.message,
                type=nexusrpc.HandlerErrorType.RESOURCE_EXHAUSTED,
            )
        elif err.status == RPCStatusCode.UNIMPLEMENTED:
            handler_err = nexusrpc.HandlerError(
                err.message,
                type=nexusrpc.HandlerErrorType.NOT_IMPLEMENTED,
            )
        elif err.status == RPCStatusCode.DEADLINE_EXCEEDED:
            handler_err = nexusrpc.HandlerError(
                err.message,
                type=nexusrpc.HandlerErrorType.UPSTREAM_TIMEOUT,
            )
        else:
            handler_err = nexusrpc.HandlerError(
                f"Unhandled RPC error status: {err.status}",
                type=nexusrpc.HandlerErrorType.INTERNAL,
            )
    else:
        handler_err = nexusrpc.HandlerError(
            "Internal handler error", type=nexusrpc.HandlerErrorType.INTERNAL
        )
    handler_err.__cause__ = err
    return handler_err


class _NexusTaskCancellation(nexusrpc.handler.OperationTaskCancellation):
    def __init__(self):
        self._thread_evt = threading.Event()
        self._async_evt = asyncio.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None

    def is_cancelled(self) -> bool:
        return self._thread_evt.is_set()

    def cancellation_reason(self) -> str | None:
        with self._lock:
            return self._reason

    def wait_until_cancelled_sync(self, timeout: float | None = None) -> bool:
        return self._thread_evt.wait(timeout)

    async def wait_until_cancelled(self) -> None:
        await self._async_evt.wait()

    def cancel(self, reason: str) -> bool:
        with self._lock:
            if self._thread_evt.is_set():
                return False
            self._reason = reason
            self._thread_evt.set()
            self._async_evt.set()
            return True


_P = ParamSpec("_P")
_T = TypeVar("_T")


class _ContextPropagatingExecutor(concurrent.futures.Executor):
    def __init__(self, executor: concurrent.futures.ThreadPoolExecutor) -> None:
        self._executor = executor

    def submit(
        self, fn: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
    ) -> concurrent.futures.Future[_T]:
        ctx = contextvars.copy_context()

        def wrapped(*a: _P.args, **k: _P.kwargs) -> _T:
            return ctx.run(fn, *a, **k)

        return self._executor.submit(wrapped, *args, **kwargs)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        return self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
