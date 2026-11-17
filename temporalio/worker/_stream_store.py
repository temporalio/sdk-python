"""How Workflow code reaches the Stream Stores of its Worker's client."""

from __future__ import annotations

import asyncio
import collections
import dataclasses
from collections.abc import Generator, Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from nexusrpc import OutputT

import temporalio.api.enums.v1
import temporalio.api.failure.v1
import temporalio.api.stream.v1
import temporalio.api.streamservice.v1
import temporalio.bridge.proto.workflow_commands
import temporalio.exceptions
import temporalio.workflow
from temporalio.client._stream_store import (
    _STREAM_OPERATIONS,
    StreamConfig,
    StreamReplaySource,
    StreamStore,
    _BridgeStreamStore,
    _request_ref,
)
from temporalio.converter import StorageDriverWorkflowInfo

from ._interceptor import StartNexusOperationInput

if TYPE_CHECKING:
    from ._workflow_instance import _WorkflowInstanceImpl


def _no_stream_config_error() -> temporalio.exceptions.ApplicationError:  # pyright: ignore[reportUnusedFunction]
    # A `StreamConfigError` would fail the Workflow Task, and its retries would fail the
    # same way for good. A Worker with no config is a setup that a user has to see, so
    # the Workflow fails instead.
    return temporalio.exceptions.ApplicationError(
        "The client of this Worker has no Stream Store. Give one in "
        "stream_config=StreamConfig(stores=[...]).",
        type="StreamConfigError",
        non_retryable=True,
    )


# The Direct calls of Workflow code, by Nexus operation name. Each has the word of an
# error message and the call that an Activity makes in its place.
_DIRECT_CALLS = {
    "ReadRecords": ("read", "Read the Stream"),
    "AppendRecords": ("append", "Append to the Stream"),
    "CloseStream": ("close", "Close the Stream"),
}


def _no_direct_call_error(
    store: StreamStore, operation: str
) -> temporalio.exceptions.ApplicationError:
    # Core makes a Workflow's read, append and close itself, and it can't call a Store
    # that runs in Python. Each retry of the task would fail the same way for good, so the
    # Workflow fails instead, as for a Worker with no Stream config.
    call, advice = _DIRECT_CALLS[operation]
    return temporalio.exceptions.ApplicationError(
        f"The Stream Store '{store.name}' has no Direct {call}, since it is written in "
        f"Python. {advice} in an Activity, or use a Store that runs on the bridge, "
        "such as the Redis Store or MemoryStreamStore.",
        type="StreamConfigError",
        non_retryable=True,
    )


def _not_a_workflow_producer_error(
    operation: str,
) -> temporalio.exceptions.StreamError:
    # Core sets the Workflow Task as the epoch of the Producer, which only a Producer of
    # kind Workflow has.
    call, _ = _DIRECT_CALLS[operation]
    return temporalio.exceptions.StreamError(
        f"A Stream {call} of Workflow code writes as a Producer of kind Workflow. "
        "Leave out producer=, or give a Producer of kind Workflow.",
        reason=temporalio.api.enums.v1.StreamErrorReason.STREAM_ERROR_REASON_INVALID,
    )


_STREAM_ERROR_DETAILS_TYPE = (
    temporalio.api.stream.v1.StreamErrorDetails.DESCRIPTOR.full_name
)


def _stream_error_from_core(  # pyright: ignore[reportUnusedFunction]
    failure: temporalio.api.failure.v1.Failure,
) -> temporalio.exceptions.StreamError | None:
    """Give the Store error of a failure that Core made, or ``None`` for another one."""
    # Core gives a Store error the token of its reason as its type, and puts the reason in
    # its details with no codec. The Local Activity path gives the Store's message alone,
    # so the token comes off the message.
    info = failure.application_failure_info
    for payload in info.details.payloads:
        if payload.metadata.get("messageType") != _STREAM_ERROR_DETAILS_TYPE.encode():
            continue
        details = temporalio.api.stream.v1.StreamErrorDetails.FromString(payload.data)
        message = failure.message.removeprefix(f"{info.type}: ")
        return temporalio.exceptions._stream_error_from_reason(details.reason, message)
    return None


def _start_direct_append(
    instance: _WorkflowInstanceImpl,
    input: StartNexusOperationInput[Any, OutputT],
    request: temporalio.api.streamservice.v1.AppendRecordsRequest,
    store_name: str,
) -> temporalio.workflow.NexusOperationHandle[OutputT]:
    """Send an append of a Workflow Producer to Core, which makes it in the Workflow Task."""
    payload_converter, _ = instance._system_nexus_converters(input)
    append = temporalio.bridge.proto.workflow_commands.AppendToStream(
        stream=temporalio.api.stream.v1.StreamRef(id=request.ref.id, store=store_name),
        bodies=[record.body for record in request.records],
        producer=request.producer,
        sequence=request.sequence,
    )
    append.producer.workflow.ClearField("epoch")
    appended = instance._start_stream_append(append)

    async def result() -> OutputT:
        response = await appended
        return payload_converter._from_transfer_value(
            temporalio.api.streamservice.v1.AppendRecordsResponse(
                first_offset=response.first_offset,
                last_offset=response.last_offset,
            ),
            input.output_type,
        )

    return _StreamStoreOperationHandle(
        instance.create_task(result(), name=f"stream store: {input.operation_name}")
    )


def _start_direct_close(
    instance: _WorkflowInstanceImpl,
    input: StartNexusOperationInput[Any, OutputT],
    request: temporalio.api.streamservice.v1.CloseStreamRequest,
    store_name: str,
) -> temporalio.workflow.NexusOperationHandle[OutputT]:
    """Send a close of a Workflow Producer to Core.

    Core records the intent in this Workflow Task, closes the Stream after the server took
    the task, and gives the result in a later task. So a failed task closes nothing.
    """
    payload_converter, _ = instance._system_nexus_converters(input)
    close = temporalio.bridge.proto.workflow_commands.CloseStream(
        stream=temporalio.api.stream.v1.StreamRef(id=request.ref.id, store=store_name),
        producer=request.producer,
    )
    close.producer.workflow.ClearField("epoch")
    closed = instance._start_stream_close(close)

    async def result() -> OutputT:
        await closed
        return payload_converter._from_transfer_value(
            temporalio.api.streamservice.v1.CloseStreamResponse(), input.output_type
        )

    return _StreamStoreOperationHandle(
        instance.create_task(result(), name=f"stream store: {input.operation_name}")
    )


def _start_direct_call(
    instance: _WorkflowInstanceImpl,
    input: StartNexusOperationInput[Any, OutputT],
    request: temporalio.api.streamservice.v1.AppendRecordsRequest
    | temporalio.api.streamservice.v1.CloseStreamRequest,
    store_name: str,
) -> temporalio.workflow.NexusOperationHandle[OutputT]:
    """Send an append or a close of Workflow code to Core.

    Core makes these calls itself, with the Workflow Task as the epoch of the Producer.

    Raises:
        temporalio.exceptions.StreamError: The Producer is not of kind Workflow.
    """
    if request.producer.WhichOneof("kind") != "workflow":
        raise _not_a_workflow_producer_error(input.operation_name)
    if isinstance(request, temporalio.api.streamservice.v1.AppendRecordsRequest):
        return _start_direct_append(instance, input, request, store_name)
    return _start_direct_close(instance, input, request, store_name)


def _start_replayed_direct_call(  # pyright: ignore[reportUnusedFunction]
    instance: _WorkflowInstanceImpl,
    input: StartNexusOperationInput[Any, OutputT],
) -> temporalio.workflow.NexusOperationHandle[OutputT] | None:
    """Replay a Direct call on a Worker with no Stream config, or give ``None``.

    The marker of an append or a close holds its result, and the marker of a read can hold
    its Records, so the replay needs no Store. A ref with no Store sends an empty name, and
    Core takes the Store of History.
    """
    if input.operation_name not in ("AppendRecords", "CloseStream", "ReadRecords"):
        return None
    payload_converter, _ = instance._system_nexus_converters(input)
    request = payload_converter._to_transfer_value(input.input)
    if isinstance(request, temporalio.api.streamservice.v1.ReadRecordsRequest):
        # A replay of a read whose marker holds its Records reads no Store.
        return _start_direct_read(
            instance, input, request, request.ref.store, records_in_history=True
        )
    if isinstance(
        request,
        (
            temporalio.api.streamservice.v1.AppendRecordsRequest,
            temporalio.api.streamservice.v1.CloseStreamRequest,
        ),
    ):
        return _start_direct_call(instance, input, request, request.ref.store)
    return None


# A client's read gives at most this many Records in a page, and a `max_records` of zero
# or less gives this many. A read of Workflow code takes the same limits.
_MAX_READ_RECORDS = 1000
# The longest `wait` of a client's read. A longer one waits this long.
_MAX_READ_WAIT = timedelta(seconds=60)


@dataclasses.dataclass(frozen=True)
class _ReadKey:
    """What a ``read`` call of Workflow code must match to take an open read."""

    store: str
    stream_id: str
    offset: bytes
    """The offset that the next page starts after. Empty is the start of the Stream."""
    wait: timedelta | None


class _DirectRead:  # pyright: ignore[reportUnusedClass]
    """One Core read of a Stream, which serves the ``read`` calls of Workflow code.

    Core gives its Records in batches. Each ``read`` call takes one page of them, and the
    read then waits for the next call under the offset of that page.
    """

    def __init__(self, seq: int) -> None:
        self.seq = seq
        self.records: collections.deque[temporalio.api.stream.v1.StreamRecord] = (
            collections.deque()
        )
        # Whether Core still reads, with no end and no close.
        self.running = True
        self.done = False
        self.error: BaseException | None = None
        # A job and an end wake the call that waits, which is at most one.
        self._waiters: list[asyncio.Future[None]] = []

    def kept(self) -> bool:
        """Whether the read can serve a later call."""
        # A failure that no call raised yet stays, so the next call raises it.
        return self.running or bool(self.records) or self.error is not None

    def take(
        self,
        records: Iterable[temporalio.api.stream.v1.StreamRecord],
        *,
        end: bool,
        done: bool,
        error: BaseException | None,
    ) -> None:
        """Keep the Records and the end of one job of Core."""
        self.records.extend(records)
        if end:
            self.running = False
            self.done = done
            self.error = error
        self._wake()

    def close(self) -> None:
        """Mark the read as closed in Core. Its buffered Records stay."""
        self.running = False
        self._wake()

    def _wake(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def page(
        self, max_records: int, max_bytes: int | None
    ) -> tuple[list[temporalio.api.stream.v1.StreamRecord], bool]:
        """Give the next page and whether the Stream is done, and wait when none is buffered.

        Raises:
            temporalio.exceptions.StreamError: The read failed.
        """
        while not self.records and self.running:
            waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            finally:
                self._waiters.remove(waiter)
        if not self.records and self.error is not None:
            error, self.error = self.error, None
            raise error
        page: list[temporalio.api.stream.v1.StreamRecord] = []
        size = 0
        while self.records and len(page) < max_records:
            record_size = self.records[0].ByteSize()
            if page and max_bytes is not None and size + record_size > max_bytes:
                break
            page.append(self.records.popleft())
            size += record_size
        return page, self.done and not self.records


def _read_limits(
    request: temporalio.api.streamservice.v1.ReadRecordsRequest,
) -> tuple[int, timedelta | None, int | None]:
    """Give the ``max_records``, ``wait`` and ``max_bytes`` of a client's read.

    Raises:
        temporalio.exceptions.StreamError: ``max_bytes`` is negative.
    """
    max_records = request.max_records
    if max_records <= 0 or max_records > _MAX_READ_RECORDS:
        max_records = _MAX_READ_RECORDS
    wait: timedelta | None = None
    if request.HasField("wait") and request.wait.ToTimedelta() > timedelta():
        wait = min(request.wait.ToTimedelta(), _MAX_READ_WAIT)
    if request.max_bytes < 0:
        raise temporalio.exceptions.StreamError(
            "max_bytes must be 0 or more",
            reason=temporalio.api.enums.v1.StreamErrorReason.STREAM_ERROR_REASON_INVALID,
        )
    return max_records, wait, request.max_bytes or None


def _start_direct_read(
    instance: _WorkflowInstanceImpl,
    input: StartNexusOperationInput[Any, OutputT],
    request: temporalio.api.streamservice.v1.ReadRecordsRequest,
    store_name: str,
    *,
    records_in_history: bool,
) -> temporalio.workflow.NexusOperationHandle[OutputT]:
    """Answer one ``read`` call of Workflow code from a Core read of the Stream."""
    instance._assert_not_read_only("start stream read")
    payload_converter, _ = instance._system_nexus_converters(input)
    max_records, wait, max_bytes = _read_limits(request)
    key = _ReadKey(
        store=store_name,
        stream_id=request.ref.id,
        offset=request.from_offset,
        wait=wait,
    )
    read = instance._take_stream_read(key)
    if read is None:
        start = temporalio.bridge.proto.workflow_commands.StartStreamRead(
            stream=temporalio.api.stream.v1.StreamRef(
                id=request.ref.id, store=store_name
            ),
            from_offset=request.from_offset,
            records_in_history=records_in_history,
        )
        # The pages come from the calls, so the Core read has no cap and no timeout.
        # With no `wait`, an idle timeout of zero gives what is in the Stream.
        start.idle_timeout.SetInParent()
        start.idle_timeout.FromTimedelta(wait or timedelta())
        read = instance._start_stream_read(start)

    async def result() -> OutputT:
        try:
            records, done = await read.page(max_records, max_bytes)
        except BaseException:
            instance._park_stream_read(read, key)
            raise
        last_offset = records[-1].offset if records else request.from_offset
        instance._park_stream_read(read, dataclasses.replace(key, offset=last_offset))
        return payload_converter._from_transfer_value(
            temporalio.api.streamservice.v1.ReadRecordsResponse(
                records=records, last_offset=last_offset, done=done
            ),
            input.output_type,
        )

    return _StreamStoreOperationHandle(
        instance.create_task(result(), name=f"stream store: {input.operation_name}")
    )


def _bridge_read_ahead_bytes(config: StreamConfig | None) -> int | None:  # pyright: ignore[reportUnusedFunction]
    """Give Core the read-ahead bound of the config, or ``None`` for Core's default."""
    return config.workflow_read_ahead_bytes if config is not None else None


def _bridge_stream_runtimes(config: StreamConfig | None) -> list[tuple[str, Any]]:  # pyright: ignore[reportUnusedFunction]
    """Give Core the bridge's object of each Store's Stream runtime, by Store name."""
    if config is None:
        return []
    return [(name, runtime._ref) for name, runtime in config._bridge_stream_runtimes()]


def _workflow_target() -> StorageDriverWorkflowInfo:
    # External storage builds the same value for a Workflow.
    info = temporalio.workflow.info()
    return StorageDriverWorkflowInfo(
        id=info.workflow_id,
        type=info.workflow_type,
        run_id=info.run_id,
        namespace=info.namespace,
    )


@dataclasses.dataclass(frozen=True)
class _WorkflowStreamStore:
    """The Worker's Stream Stores as Workflow code reaches them."""

    config: StreamConfig
    """The Stream config of the Worker's client."""

    @classmethod
    def from_config(cls, config: StreamConfig | None) -> _WorkflowStreamStore | None:
        """Build it from the Stream config of a client, or give ``None`` when it has none."""
        return cls(config=config) if config is not None else None

    def start_operation(
        self,
        instance: _WorkflowInstanceImpl,
        input: StartNexusOperationInput[Any, OutputT],
    ) -> temporalio.workflow.NexusOperationHandle[OutputT]:
        """Answer a Stream call of Workflow code with the Store that the call goes to."""
        stream_operation = _STREAM_OPERATIONS[input.operation_name]
        payload_converter, _ = instance._system_nexus_converters(input)
        request = payload_converter._to_transfer_value(input.input)
        ref = _request_ref(input.input)
        # The choice runs in Workflow code, so a ref of a Store that the config doesn't
        # have raises `StreamConfigError` here, with no I/O, and fails the Workflow Task.
        store = self.config._store_for(ref, _workflow_target())
        options = self.config.workflow_call_options
        if isinstance(request, temporalio.api.streamservice.v1.ReadRecordsRequest):
            instance._assert_not_read_only("start stream read")
            if not isinstance(store, _BridgeStreamStore):
                raise _no_direct_call_error(store, input.operation_name)
            return _start_direct_read(
                instance,
                input,
                request,
                store.name,
                records_in_history=(
                    self.config.workflow_read_replay_from is StreamReplaySource.HISTORY
                ),
            )
        if isinstance(
            request,
            (
                temporalio.api.streamservice.v1.AppendRecordsRequest,
                temporalio.api.streamservice.v1.CloseStreamRequest,
            ),
        ):
            if not isinstance(store, _BridgeStreamStore):
                raise _no_direct_call_error(store, input.operation_name)
            return _start_direct_call(instance, input, request, store.name)

        # A create and a describe run in the Store's Local Activity.
        async def result() -> OutputT:
            try:
                response = await store.call_from_workflow(
                    input.operation_name, request, options
                )
            except temporalio.exceptions.ApplicationError as err:
                # Core hands back a failed Local Activity's error without a wrapper.
                stream_error = temporalio.exceptions._stream_error_from_failure(err)
                if stream_error is not None:
                    raise stream_error from None
                raise
            except temporalio.exceptions.ActivityError as err:
                # Workflow code gets the Store's error, not the Local Activity's wrapper.
                if err.cause is not None:
                    stream_error = temporalio.exceptions._stream_error_from_failure(
                        err.cause
                    )
                    if stream_error is not None:
                        raise stream_error from None
                if isinstance(
                    err.cause,
                    (
                        temporalio.exceptions.ApplicationError,
                        temporalio.exceptions.TimeoutError,
                    ),
                ):
                    raise err.cause
                raise
            return payload_converter._from_transfer_value(
                stream_operation.check_response(store, response), input.output_type
            )

        return _StreamStoreOperationHandle(
            instance.create_task(result(), name=f"stream store: {input.operation_name}")
        )


class _StreamStoreOperationHandle(temporalio.workflow.NexusOperationHandle[OutputT]):
    def __init__(self, task: asyncio.Task[OutputT]) -> None:
        self._task = task

    def cancel(self) -> bool:
        return self._task.cancel()

    def __await__(self) -> Generator[Any, Any, OutputT]:
        return self._task.__await__()

    @property
    def operation_token(self) -> str | None:
        return None
