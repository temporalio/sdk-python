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
import temporalio.api.sdk.v1 as sdk
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
    _request_ref,
    _sdk_request_type,
    _StoreCaller,
)
from temporalio.converter import StorageDriverWorkflowInfo
from temporalio.exceptions import StreamError

from ._interceptor import StartLocalActivityInput, StartNexusOperationInput

if TYPE_CHECKING:
    from ._workflow_instance import _WorkflowInstanceImpl

_STORE_CALL_ACTIVITY = "__temporal_stream_store_call"


@dataclass
class _Read:
    waiter: asyncio.Future[Any]
    page_type: Any


class _WorkflowStreams:  # pyright: ignore[reportUnusedClass]
    """Use Local Activities for create and describe because Core has no commands
    for those operations.
    """

    def __init__(self, instance: _WorkflowInstanceImpl) -> None:
        self._instance = instance
        self._appends: dict[int, asyncio.Future[Any]] = {}
        self.task_error: Exception | None = None
        # Cancelled reads still receive one result from Core.
        self._reads: dict[int, _Read] = {}

    def start(
        self, input: StartNexusOperationInput[Any, OutputT]
    ) -> temporalio.workflow.NexusOperationHandle[OutputT]:
        """Schedule before returning so command order matches call order.

        Cancellation stops the caller's wait. Core still completes appends and
        reads, so their later results must be discarded.
        """
        instance = self._instance
        instance._assert_not_read_only("start a Stream call")
        operation = input.operation_name
        converters = (
            instance._context_free_payload_converter,
            instance._context_free_failure_converter,
        )
        model = input.input
        result_type: Any = input.output_type
        request = temporalio.nexus.system._support.model_to_proto(model, *converters)
        # Reject missing type information before a result could fail the task.
        if isinstance(request, sdk.ReadRecordsRequest) and typing.get_args(
            result_type
        ) == (object,):
            raise TypeError(
                "Cannot read a Stream with no body type. Give get_stream a "
                "body_type, or a StreamRef with a type argument."
            )
        store = self._store(_request_ref(request)).name()
        result = _start_call(self, store, operation, request, result_type)

        async def call() -> OutputT:
            # `asyncio.wait` ends at a cancel of this task. `await result` would pass the
            # cancel on and wait for the cancelled Local Activity.
            await asyncio.wait([result])
            if isinstance(request, sdk.ReadRecordsRequest):
                return result.result()
            return temporalio.nexus.system._support.model_from_proto(
                self._response(operation, request, result),
                result_type,
                *converters,
            )

        def stop(_: asyncio.Task[OutputT]) -> None:
            if result.done():
                return
            result.cancel()

        task = instance.create_task(call(), name=f"stream: {operation}")
        # A callback also runs for a task that is cancelled before its first step.
        task.add_done_callback(stop)
        return _StreamCall(task)

    def _store(self, ref: sdk.StreamRef) -> StreamStore:
        config = self._stream_config()
        if config is None:
            raise self._fail_task(
                ValueError(
                    "Worker or Replayer has no stream_config, which every "
                    "Stream call requires, also in a replay."
                )
            )
        info = self._instance._info
        target = StorageDriverWorkflowInfo(
            id=info.workflow_id,
            run_id=info.run_id,
            type=info.workflow_type,
            namespace=info.namespace,
        )
        try:
            return config._store_for(ref, target, info.namespace)
        except Exception as err:
            raise self._fail_task(err)

    def resolve_append(
        self, job: temporalio.bridge.proto.workflow_activation.ResolveStreamAppend
    ) -> None:
        future = self._appends.pop(job.seq, None)
        if future is None:
            raise RuntimeError(f"Failed finding Stream append for sequence {job.seq}")
        # Core completes writes even after their caller is cancelled.
        if future.done():
            return
        if job.HasField("failed"):
            future.set_exception(self._error(job.failed))
        else:
            future.set_result(job.appended)

    def resolve_read(
        self, job: temporalio.bridge.proto.workflow_activation.ResolveStreamRead
    ) -> None:
        read = self._reads.pop(job.seq, None)
        if read is None:
            raise RuntimeError(f"Failed finding Stream read for sequence {job.seq}")
        if read.waiter.done():
            return
        if job.HasField("failed"):
            read.waiter.set_exception(self._error(job.failed))
            return
        page = proto.ReadRecordsResponse(
            records=job.page.records, last_offset=job.page.last_offset
        )
        # Conversion failures must fail the task before Workflow code resumes.
        read.waiter.set_result(
            temporalio.nexus.system._support.model_from_proto(
                page,
                read.page_type,
                self._instance._context_free_payload_converter,
                self._instance._context_free_failure_converter,
            )
        )

    def _error(self, failure: temporalio.api.failure.v1.Failure) -> BaseException:
        instance = self._instance
        return _stream_error_from_failure(failure) or (
            instance._context_free_failure_converter.from_failure(
                failure, instance._context_free_payload_converter
            )
        )

    def _schedule_store_call(
        self,
        store: str,
        operation: str,
        request: google.protobuf.message.Message,
        request_id: str | None,
    ) -> asyncio.Future[Any]:
        instance = self._instance
        activity = instance._outbound.start_local_activity(
            StartLocalActivityInput(
                activity=_STORE_CALL_ACTIVITY,
                args=(operation, store, request.SerializeToString(), request_id),
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
        request: google.protobuf.message.Message,
        result: asyncio.Future[Any],
    ) -> google.protobuf.message.Message:
        if isinstance(request, sdk.AppendRecordsRequest):
            appended = result.result()
            return proto.AppendRecordsResponse(
                first_offset=appended.first_offset, last_offset=appended.last_offset
            )
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
        # Replay must retain the Store recorded by create.
        if isinstance(request, sdk.CreateStreamRequest):
            return sdk.CreateStreamResponse.FromString(answer)
        return proto.DescribeStreamResponse.FromString(answer)

    def _fail_task(self, error: Exception) -> Exception:
        """Prevent caught errors from recording an unserved call as successful."""
        self.task_error = self._instance._current_activation_error = error
        return error

    def _stream_config(self) -> StreamConfig | None:
        functions = self._instance._extern_functions
        return cast(
            Callable[[], StreamConfig | None], functions["__temporal_stream_config"]
        )()


def _start_call(
    streams: _WorkflowStreams,
    store: str,
    operation: str,
    request: google.protobuf.message.Message,
    result_type: Any,
) -> asyncio.Future[Any]:
    """Use the selected Store in commands so replay can detect routing changes."""
    instance = streams._instance
    stream = sdk.StreamRef(id=_request_ref(request).id, store=store)
    if isinstance(request, sdk.AppendRecordsRequest):
        seq = instance._next_seq("stream_append")
        append = instance._add_command().append_to_stream
        append.seq = seq
        append.stream.CopyFrom(stream)
        append.bodies.extend(record.body for record in request.records)
        result = streams._appends[seq] = instance.create_future()
        return result
    if isinstance(request, sdk.ReadRecordsRequest):
        seq = instance._next_seq("stream_read")
        instance._add_command().read_stream.CopyFrom(
            temporalio.bridge.proto.workflow_commands.ReadStream(
                seq=seq,
                stream=stream,
                from_offset=request.from_offset,
                wait=request.wait if request.HasField("wait") else None,
                max_records=request.max_records,
                max_bytes=request.max_bytes,
            )
        )
        result = instance.create_future()
        streams._reads[seq] = _Read(result, result_type)
        return result
    # Runs in a chain must retry the same create.
    request_id = None
    if isinstance(request, sdk.CreateStreamRequest):
        request_id = instance._info.first_execution_run_id
    return streams._schedule_store_call(store, operation, request, request_id)


class _StreamCall(temporalio.workflow.NexusOperationHandle[OutputT]):
    """Cancel the caller's wait without waiting for the Store call to finish."""

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
    operation: str, store: str, request: bytes, request_id: str | None
) -> bytes:
    """Record the selected Store in create results so replay keeps the same ref."""
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
    stream_request = _sdk_request_type(operation).FromString(request)
    response = await _call_store(
        stream_config._store(store),
        stream_config._limits(),
        client.data_converter.payload_codec,
        operation,
        stream_request,
        request_id,
    )
    return response.SerializeToString()


def _stream_error_from_failure(
    failure: temporalio.api.failure.v1.Failure,
) -> StreamError | None:
    """Accept Core's binary details and the default converter's JSON details."""
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
    if stream_config is None:
        return set(), None
    return set(stream_config._by_name), stream_config._limits()


def _bridge_streams(  # pyright: ignore[reportUnusedFunction]
    stream_config: Callable[[], StreamConfig | None],
    read_time: timedelta | None,
) -> temporalio.bridge.streams.WorkerStreams:
    """Resolve Store instances at call time so client replacement takes effect."""

    def store(name: str) -> StreamStore:
        current = stream_config()
        assert current
        return current._store(name)

    names, limits = _for_core(stream_config())
    return temporalio.bridge.streams.WorkerStreams(
        limits=limits,
        # Client swaps must affect later Store calls.
        stores=[
            (name, _StoreCaller(functools.partial(store, name)))
            for name in sorted(names)
        ],
        read_time_millis=int(1000 * read_time.total_seconds())
        if read_time is not None
        else None,
    )


def _check_stream_swap(  # pyright: ignore[reportUnusedFunction]
    existing: StreamConfig | None, new: StreamConfig | None
) -> None:
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
