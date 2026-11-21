"""Stream calls of Workflow code."""

from __future__ import annotations

import asyncio
import functools
import typing
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import google.protobuf.json_format
import google.protobuf.message
from nexusrpc import OutputT

import temporalio.activity
import temporalio.api.failure.v1
import temporalio.api.stream.v1
import temporalio.api.streamservice.v1 as proto
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_commands
import temporalio.bridge.streams
import temporalio.common
import temporalio.exceptions
import temporalio.nexus.system._support
import temporalio.workflow
from temporalio.api.enums.v1 import StreamErrorReason
from temporalio.client._stream_store import (
    StreamConfig,
    StreamStore,
    _call_store,
    _operation,
    _StoreCaller,
)
from temporalio.converter import StorageDriverWorkflowInfo
from temporalio.exceptions import StreamError
from temporalio.nexus.system.stream_service import StreamRef

from ._interceptor import StartLocalActivityInput, StartNexusOperationInput

if TYPE_CHECKING:
    from ._workflow_instance import _WorkflowInstanceImpl

_STORE_CALL_ACTIVITY = "__temporal_stream_store_call"


@dataclass
class _Read:
    """The read of one ``read_page`` call."""

    after: bytes
    """The offset that the read started from."""

    waiter: asyncio.Future[Any]
    """Gets the page of the call."""

    page_type: Any
    """The type of the page that the call returns."""


class _WorkflowStreams:  # pyright: ignore[reportUnusedClass]
    """The Stream calls of one Workflow instance.

    An append, a close and a read are commands of SDK Core, which makes the Store
    calls and gives each result in a job. Core has no command for a create or a
    describe, so those run in a Local Activity of the SDK.
    """

    def __init__(self, instance: _WorkflowInstanceImpl) -> None:
        self._instance = instance
        # Each append and close whose job hasn't come, by seq
        self._appends: dict[int, asyncio.Future[Any]] = {}
        self._closes: dict[int, asyncio.Future[Any]] = {}
        self.task_error: Exception | None = None
        """The error of a call that must fail the Workflow Task, if one came."""
        # Each read whose job hasn't come, by seq. A cancelled read gets its one job too.
        self._reads: dict[int, _Read] = {}

    def start(
        self, input: StartNexusOperationInput[Any, OutputT]
    ) -> temporalio.workflow.NexusOperationHandle[OutputT]:
        """Start a Stream operation and return its handle.

        The command of the call is added before this returns, so commands are in
        the order of the calls. Cancelling the handle ends its wait immediately.
        Core can't stop an append or a close, so those still run. A cancelled
        read sends ``RequestCancelStreamRead``, and the job that still comes for
        it is dropped.
        """
        instance = self._instance
        instance._assert_not_read_only("start a Stream call")
        operation = input.operation_name
        converters = (
            instance._context_free_payload_converter,
            instance._context_free_failure_converter,
        )
        model = input.input
        # The terminal of each Stream operation gives its output type.
        result_type: Any = input.output_type
        request = temporalio.nexus.system._support.model_to_proto(model, *converters)
        # The future of the job of the call, or the task of its Local Activity
        result: asyncio.Future[Any]
        # The seq of the call's read, or 0 for another operation
        read_seq = 0
        if isinstance(request, proto.AppendRecordsRequest):
            stream = self._stream(model.ref)
            seq = instance._next_seq("stream_append")
            append = instance._add_command().append_to_stream
            append.seq = seq
            append.stream.CopyFrom(stream)
            append.bodies.extend(record.body for record in request.records)
            result = self._appends[seq] = instance.create_future()
        elif isinstance(request, proto.CloseStreamRequest):
            stream = self._stream(model.ref)
            seq = instance._next_seq("stream_close")
            close = instance._add_command().close_stream
            close.seq = seq
            close.stream.CopyFrom(stream)
            result = self._closes[seq] = instance.create_future()
        elif isinstance(request, proto.ReadRecordsRequest):
            # The converter refuses a body of no type, and the job of the page would
            # then fail the Workflow Task.
            if typing.get_args(result_type) == (object,):
                raise TypeError(
                    "Cannot read a Stream with no body type. Give get_stream a "
                    "body_type, or a StreamRef with a type argument."
                )
            stream = self._stream(model.ref)
            read_seq = instance._next_seq("stream_read")
            instance._add_command().read_stream.CopyFrom(
                temporalio.bridge.proto.workflow_commands.ReadStream(
                    seq=read_seq,
                    stream=stream,
                    from_offset=request.from_offset,
                    wait=request.wait if request.HasField("wait") else None,
                    max_records=request.max_records,
                    max_bytes=request.max_bytes,
                )
            )
            result = instance.create_future()
            self._reads[read_seq] = _Read(request.from_offset, result, result_type)
        else:
            # The Local Activity picks the Store of a ref with no Store name. A replay
            # has its result, so a replay asks no selector for it.
            result = self._schedule_store_call(
                operation, model.ref.store or "", request
            )

        async def call() -> OutputT:
            # `asyncio.wait` ends at a cancel of this task. `await result` would pass the
            # cancel on and wait for the cancelled Local Activity.
            await asyncio.wait([result])
            if isinstance(request, proto.ReadRecordsRequest):
                return result.result()
            return temporalio.nexus.system._support.model_from_proto(
                self._response(operation, model, request, result),
                result_type,
                *converters,
            )

        def stop(_: asyncio.Task[OutputT]) -> None:
            if result.done():
                return
            result.cancel()
            # Only a read has a cancel command. An eviction sends no command.
            if read_seq and not instance._deleting:
                instance._add_command().request_cancel_stream_read.seq = read_seq

        task = instance.create_task(call(), name=f"stream: {operation}")
        # A callback also runs for a task that is cancelled before its first step.
        task.add_done_callback(stop)
        return _StreamCall(task)

    def resolve_append(
        self, job: temporalio.bridge.proto.workflow_activation.ResolveStreamAppend
    ) -> None:
        self._resolve(self._appends, job.seq, job.appended, job)

    def resolve_close(
        self, job: temporalio.bridge.proto.workflow_activation.ResolveStreamClose
    ) -> None:
        self._resolve(self._closes, job.seq, None, job)

    def resolve_read(
        self, job: temporalio.bridge.proto.workflow_activation.ResolveStreamRead
    ) -> None:
        read = self._reads.pop(job.seq, None)
        if read is None:
            raise RuntimeError(f"Failed finding Stream read for sequence {job.seq}")
        # The job of a cancelled call is dropped. A cancel job is dropped too if it comes first,
        # as in a replay, since only the cancel of the call may end its wait.
        if read.waiter.done() or job.HasField("cancelled"):
            return
        if job.HasField("failed"):
            read.waiter.set_exception(self._error(job.failed))
            return
        records = job.page.records
        page = proto.ReadRecordsResponse(
            records=records,
            last_offset=records[-1].offset if records else read.after,
            done=job.page.done,
        )
        # A body that the converter refuses fails the Workflow Task, as a result of a
        # Local Activity does.
        read.waiter.set_result(
            temporalio.nexus.system._support.model_from_proto(
                page,
                read.page_type,
                self._instance._context_free_payload_converter,
                self._instance._context_free_failure_converter,
            )
        )

    def _resolve(
        self,
        calls: dict[int, asyncio.Future[Any]],
        seq: int,
        result: Any,
        job: Any,
    ) -> None:
        future = calls.pop(seq, None)
        if future is None:
            raise RuntimeError(f"Failed finding Stream call for sequence {seq}")
        # A cancelled call no longer waits, and Core still gives the result of its write.
        if future.done():
            return
        if job.HasField("failed"):
            future.set_exception(self._error(job.failed))
        else:
            future.set_result(result)

    def _error(self, failure: temporalio.api.failure.v1.Failure) -> BaseException:
        instance = self._instance
        return _stream_error_from_failure(failure) or (
            instance._context_free_failure_converter.from_failure(
                failure, instance._context_free_payload_converter
            )
        )

    def _schedule_store_call(
        self, operation: str, store: str, request: google.protobuf.message.Message
    ) -> asyncio.Future[Any]:
        """Schedule the SDK's Local Activity for a create or a describe."""
        instance = self._instance
        # A Worker whose client has no Stream config has no such Local Activity, so the
        # call would wait with no end. A replay takes the result from History.
        if not instance._is_replaying and self._stream_config() is None:
            raise self._fail_task(
                ValueError(
                    "Client of this Worker has no stream_config, which every "
                    "Stream call requires."
                )
            )
        activity = instance._outbound.start_local_activity(
            StartLocalActivityInput(
                activity=_STORE_CALL_ACTIVITY,
                args=(store, operation, request.SerializeToString()),
                activity_id=None,
                # A call that hangs ends before the heartbeat of the Workflow Task, and an
                # outage fails the task immediately, so neither writes into History.
                schedule_to_close_timeout=instance._info.task_timeout / 2,
                schedule_to_start_timeout=None,
                start_to_close_timeout=None,
                retry_policy=temporalio.common.RetryPolicy(maximum_attempts=1),
                local_retry_threshold=None,
                cancellation_type=temporalio.workflow.ActivityCancellationType.TRY_CANCEL,
                headers={},
                summary=None,
                arg_types=None,
                ret_type=None,
            )
        )
        # Nothing awaits the Activity once its call is cancelled.
        activity.add_done_callback(lambda done: done.cancelled() or done.exception())
        return activity

    def _response(
        self,
        operation: str,
        model: Any,
        request: google.protobuf.message.Message,
        result: asyncio.Future[Any],
    ) -> google.protobuf.message.Message:
        """Return the response message of a call whose ``result`` is done."""
        if isinstance(request, proto.AppendRecordsRequest):
            appended = result.result()
            return proto.AppendRecordsResponse(
                first_offset=appended.first_offset, last_offset=appended.last_offset
            )
        if isinstance(request, proto.CloseStreamRequest):
            result.result()
            return proto.CloseStreamResponse()
        try:
            answer = result.result()
        except temporalio.exceptions.FailureError as err:
            # A refusal of the Store comes as the failure of the attempt. A timeout comes
            # inside an Activity failure, which has no Stream details.
            error = _stream_error_from_failure(err.failure) if err.failure else None
            if (
                error is None
                or error.reason
                == StreamErrorReason.STREAM_ERROR_REASON_STORE_UNAVAILABLE
            ):
                # Workflow code could catch an error of this call, so the task fails and
                # nothing goes into History, as for an outage during an append.
                outage = RuntimeError(f"Stream Store call {operation} failed")
                outage.__cause__ = err
                raise self._fail_task(outage)
            raise error from err
        if isinstance(request, proto.CreateStreamRequest):
            # The created Stream takes the ref of its request. The name is the one in
            # History, so a replay gives the same ref under any Stream config.
            model.ref.store = answer["store"]
            return proto.CreateStreamResponse(created=answer["created"])
        return proto.DescribeStreamResponse.FromString(answer)

    def _stream(self, ref: StreamRef[Any]) -> temporalio.api.stream.v1.StreamRef:
        """Return the ref of a command, with the name of the Store that SDK Core calls.

        A ref with no Store name gets the Store that the config of the Worker's
        client picks, and an empty name if that client has no Stream config.
        """
        store = ref.store or ""
        config = self._stream_config()
        if config and not store:
            info = self._instance._info
            target = StorageDriverWorkflowInfo(
                id=info.workflow_id,
                run_id=info.run_id,
                type=info.workflow_type,
                namespace=info.namespace,
            )
            try:
                store = config._store_for(ref, target).name()
            except Exception as err:
                raise self._fail_task(err)
        return temporalio.api.stream.v1.StreamRef(id=ref.id, store=store)

    def _fail_task(self, error: Exception) -> Exception:
        """Make ``error`` the failure of the Workflow Task and return it.

        Workflow code that catches the error of a call that the Worker can't serve
        would go on with no Stream call in History. So the task fails, whatever the
        failure exception types of the Worker and of the Workflow are.
        """
        self.task_error = self._instance._current_activation_error = error
        return error

    def _stream_config(self) -> StreamConfig | None:
        functions = self._instance._extern_functions
        return cast(
            Callable[[], StreamConfig | None], functions["__temporal_stream_config"]
        )()


class _StreamCall(temporalio.workflow.NexusOperationHandle[OutputT]):
    """Handle of a Stream call, which a task of the Workflow instance runs."""

    def __init__(self, task: asyncio.Task[OutputT]) -> None:
        self._task = task

    @property
    def operation_token(self) -> str | None:
        return None

    def __await__(self) -> Generator[Any, Any, OutputT]:
        return self._task.__await__()

    def cancel(self) -> bool:
        return self._task.cancel()


@temporalio.activity.defn(name=_STORE_CALL_ACTIVITY)
async def _stream_store_call(  # pyright: ignore[reportUnusedFunction]
    store: str, operation: str, request: bytes
) -> Any:
    """Call a Stream Store of the Worker's client for a create or a describe of
    Workflow code. ``store`` is the Store name of the call's ref, or empty.

    Returns the name of the Store that it called and ``created`` for a create, so
    that History has the Store of the created Stream, and the encoded response for
    a describe.
    """
    info = temporalio.activity.info()
    if not info.is_local or operation not in ("CreateStream", "DescribeStream"):
        raise temporalio.exceptions.ApplicationError(
            f"{_STORE_CALL_ACTIVITY} is an Activity of the SDK, which only runs "
            "a create or a describe of a Stream as a Local Activity",
            non_retryable=True,
        )
    client = temporalio.activity.client()
    stream_config = client._config["stream_config"]
    if stream_config is None:
        raise ValueError("Client of this Worker has no stream_config.")
    _, request_type, _ = _operation(operation)
    stream_request: Any = request_type.FromString(request)
    # The selector gets the Workflow as its target, as for an append of that Workflow.
    target = StorageDriverWorkflowInfo(
        id=info.workflow_id,
        run_id=info.workflow_run_id,
        type=info.workflow_type,
        namespace=info.namespace,
    )
    picked = stream_config._store_for(
        StreamRef(id=stream_request.stream_id, store=store or None), target
    )
    response = await _call_store(
        picked,
        stream_config._limits(),
        client.data_converter.payload_codec,
        operation,
        stream_request,
    )
    if isinstance(response, proto.CreateStreamResponse):
        return {"store": picked.name(), "created": response.created}
    return response.SerializeToString()


def _stream_error_from_failure(
    failure: temporalio.api.failure.v1.Failure,
) -> StreamError | None:
    """Return the Stream error of a failure, or ``None`` if it isn't one.

    The failure of a Stream call has a
    ``temporal.api.stream.v1.StreamErrorDetails`` message in its details. A
    reason that this SDK doesn't know becomes
    ``STREAM_ERROR_REASON_UNSPECIFIED``.
    """
    details = temporalio.api.stream.v1.StreamErrorDetails()
    for payload in failure.application_failure_info.details.payloads:
        if payload.metadata.get("messageType") != details.DESCRIPTOR.full_name.encode():
            continue
        # Core writes the details as binary, and the default converter as JSON.
        if payload.metadata.get("encoding") == b"json/protobuf":
            try:
                google.protobuf.json_format.Parse(payload.data, details)
            except google.protobuf.json_format.ParseError:
                # The JSON has a reason by a name that this SDK doesn't know.
                pass
        else:
            details.ParseFromString(payload.data)
        reason = details.reason
        if reason not in StreamErrorReason.values():
            reason = StreamErrorReason.STREAM_ERROR_REASON_UNSPECIFIED
        error = StreamError(failure.message, reason=reason)
        error._failure = failure
        return error
    return None


def _for_core(
    stream_config: StreamConfig | None,
) -> tuple[set[str], tuple[int, int, int] | None]:
    """Return the Store names and the limits of a Stream config, which Core has
    from the start of a worker.
    """
    if stream_config is None:
        return set(), None
    return {store.name() for store in stream_config.stores}, stream_config._limits()


def _bridge_streams(  # pyright: ignore[reportUnusedFunction]
    stream_config: Callable[[], StreamConfig | None],
    read_max_wait: timedelta | None,
) -> temporalio.bridge.streams.WorkerStreams:
    """Return what the bridge needs for the Stream Stores of a worker.

    ``stream_config`` gives the Stream config that the worker has at a call,
    which is the one of another client after a client swap. ``read_max_wait`` is
    the longest wait of one Store call of a read, or ``None`` for Core's default.
    """

    def store(name: str) -> StreamStore:
        current = stream_config()
        assert current
        return current._store(name)

    names, limits = _for_core(stream_config())
    return temporalio.bridge.streams.WorkerStreams(
        limits=limits,
        # Each caller finds its Store in the config that the worker has at the call.
        stores=[
            (name, _StoreCaller(functools.partial(store, name)))
            for name in sorted(names)
        ],
        read_max_wait_millis=int(1000 * read_max_wait.total_seconds())
        if read_max_wait is not None
        else None,
    )


def _check_stream_swap(  # pyright: ignore[reportUnusedFunction]
    existing: StreamConfig | None, new: StreamConfig | None
) -> None:
    """Raise if the new client of a worker has other Stream Stores or other
    limits than the existing one.
    """
    names, limits = _for_core(existing)
    new_names, new_limits = _for_core(new)
    if new_names != names:
        raise ValueError(
            "New client does not have the Stream Stores of the existing client"
        )
    if new_limits != limits:
        raise ValueError(
            "New client does not have the Stream limits of the existing client"
        )
