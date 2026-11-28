"""Stream Stores and the Stream config of a client."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import google.protobuf.message
import nexusrpc

import temporalio.activity
import temporalio.api.sdk.v1 as sdk
import temporalio.bridge.streams

# pydoctor reads this module before the client package, for the worker modules that
# import it. A `from` import of a sibling here would lose the package's re-exports.
import temporalio.client
import temporalio.nexus.system._support
from temporalio.api.enums.v1 import StreamErrorReason
from temporalio.api.streamservice.v1 import (
    AppendRecordsRequest,
    AppendRecordsResponse,
    CreateStreamRequest,
    CreateStreamResponse,
    DescribeStreamRequest,
    DescribeStreamResponse,
    ReadRecordsRequest,
    ReadRecordsResponse,
)
from temporalio.converter import (
    PayloadCodec,
    StorageDriverActivityInfo,
    StorageDriverWorkflowInfo,
)
from temporalio.converter._payload_converter import _get_transfer_type_converter
from temporalio.exceptions import StreamError
from temporalio.nexus.system.stream_service import StreamService

logger = logging.getLogger(__name__)

_definition = nexusrpc.get_service_definition(StreamService)
assert _definition
_STREAM_SERVICE: nexusrpc.ServiceDefinition = _definition

_READ_ANSWER_TIME = timedelta(seconds=1)
"""The time that a client read leaves its Store to answer before the
``rpc_timeout``. A tenth of a shorter timeout is used instead."""


class StreamStore(ABC):
    """Base class for a Store that keeps Stream Records.

    Methods use request and response messages from
    :py:mod:`temporalio.api.streamservice.v1`. Requests contain a Stream id.
    The SDK routes each ``StreamRef`` to its registered Store and sets the Store
    name on refs returned by create. Stores don't read or set ``StreamRef.store``.

    Workflow execution and replay depend on these contracts.

    Retention:

    * Retention starts at create and restarts after an append that writes a batch.
      Retries and reads don't extend it. The Store sets the retention period.
    * An expired Stream has no Records and rejects calls with ``EXPIRED``.
    * Streams have no stored end. Applications use their own terminal Record.

    Records and offsets:

    * Keep each body as received. The payload codec has already encoded it.
    * Write each append batch atomically, with contiguous Records in request order.
    * Record offsets are nonempty and sort bytewise in Record order.
    * After a response returns an offset, no later write can insert a Record at
      or below it. This also applies to concurrent writes.
    * Keep Records unchanged until the Stream expires.

    Producers and attempts:

    * A Producer identifies a Workflow run, Activity, Local Activity or client.
      Its kind and ids determine its identity.
    * An attempt identifies a Producer's execution attempt, such as a Workflow
      Task or Activity attempt. Every Record retains its Producer and attempt.
    * Fencing is optional. A Store that fences rejects an earlier attempt once
      a later attempt of that Producer has written to the Stream.

    Reject calls with :py:class:`temporalio.exceptions.StreamError` and its reason.
    These docs omit the ``STREAM_ERROR_REASON_`` prefix from reason names.
    Other exceptions become ``STORE_UNAVAILABLE`` errors. Workflow append and
    read refusals record messages of at most 4 KiB in History. All Stream marker
    data in an activation must fit within 1 MiB. Keep messages short to avoid
    failing the Workflow Task with ``StreamMarkerTooLarge``.

    Calls can run concurrently on the event loop. Cancellation can follow a
    timeout, eviction or Worker shutdown. A cancelled call may have taken effect.
    The Store must remain usable, and overlapping retries must not duplicate a
    batch.

    SDK Core validates and retries Workflow appends and reads during outages.
    Other calls make one attempt. For those calls, the SDK checks only append
    limits from :py:class:`StreamConfig`. Stores must perform any other checks
    they require.

    .. warning::
        This API is experimental and subject to change.
    """

    @abstractmethod
    def name(self) -> str:
        """Return the Store name registered in :py:class:`StreamConfig`.

        The name must remain stable. Separate instances of one Store class can
        use different names.
        """
        raise NotImplementedError

    @abstractmethod
    async def create_stream(
        self, request: CreateStreamRequest, *, request_id: str
    ) -> CreateStreamResponse:
        """Create a Stream, or reuse it if the conflict policy permits.

        Retries use the same ``request_id``. An empty id never identifies a retry.
        The response contains only ``created``.

        For an existing Stream, check these conditions in order.

        1. An expired Stream fails with ``EXPIRED``.
        2. A matching create ``request_id`` returns ``created=True``.
        3. ``USE_EXISTING`` returns ``created=False``. ``FAIL`` or an unset policy
           fails with ``ALREADY_EXISTS``.

        Only a new Stream starts retention. Reusing a Stream doesn't extend it.
        """
        raise NotImplementedError

    @abstractmethod
    async def append_records(
        self, request: AppendRecordsRequest, *, request_id: str
    ) -> AppendRecordsResponse:
        """Append one batch from one Producer.

        Retries of one append use the same ``request_id``.
        Retain recent request ids for each Producer, including at least its last
        batch. Matching Producer, attempt and ``request_id`` identify a resend.
        An empty request id never identifies a resend.

        Check these conditions in order.

        1. Missing or expired Streams fail with ``NOT_FOUND`` or ``EXPIRED``.
        2. A resend returns the original offsets without comparing or writing
           Records.
        3. If fencing is enabled, an attempt older than the latest writer attempt
           fails with ``FENCED``.

        Return the first and last Record offsets. Only a new batch extends
        retention. A resend doesn't extend it.
        """
        raise NotImplementedError

    @abstractmethod
    async def read_records(self, request: ReadRecordsRequest) -> ReadRecordsResponse:
        """Read one page after ``from_offset``, in Stream order.

        * Apply ``max_records`` and ``max_bytes``. Zero uses the Store's default.
          Include the first Record even if it exceeds ``max_bytes``.
        * ``last_offset`` is the cursor for the next read. If omitted, the SDK
          uses the last Record's offset, or ``from_offset`` for an empty page.
        * If no Records are available, wait until one arrives or ``wait`` expires.
          An unset wait returns immediately. The SDK converts zero to an unset
          wait. Returning empty pages before a requested wait expires can cause
          callers to poll repeatedly.
        * Accept any offset returned by a response. Reject ``from_offset`` with
          ``OFFSET_TOO_HIGH`` only if it exceeds the end of the Stream.
        """
        raise NotImplementedError

    @abstractmethod
    async def describe_stream(
        self, request: DescribeStreamRequest
    ) -> DescribeStreamResponse:
        """Return a ``last_offset`` at or beyond the newest Record's offset."""
        raise NotImplementedError


@dataclass(frozen=True)
class StreamSelectContext:
    """Context passed to :py:attr:`StreamConfig.store_selector` calls.

    .. warning::
        This API is experimental and subject to change.
    """

    id: str
    """The id of the Stream of the call."""

    target: StorageDriverActivityInfo | StorageDriverWorkflowInfo | None = None
    """The Workflow or the Activity that makes the call, or ``None`` for other
    client code."""

    namespace: str | None = None
    """The namespace of the client or Worker that makes the call.

    It is set even when :py:attr:`target` is ``None``, so that a selector can pick a
    Store by namespace for plain client code too."""


@dataclass(frozen=True)
class StreamConfig:
    """Stream Stores shared by a client and its Workers.

    Appends that exceed :py:attr:`max_record_bytes`, :py:attr:`max_batch_bytes`
    or :py:attr:`max_batch_records` fail with ``STREAM_ERROR_REASON_TOO_LARGE``
    before reaching the Store. ``rpc_metadata`` has no effect on Store calls.

    Streams have no close. Retention starts at create and restarts after each
    append that writes a batch. Applications use their own terminal Record.

    In client, Activity and Workflow code, ``read`` and ``read_pages`` continue
    after empty pages. ``read_pages`` yields those pages. Callers must stop the
    iterator, for example with ``break`` or a timeout. An unset ``wait`` makes the
    first request immediate. Later requests wait up to one minute. An explicit
    wait applies to every request. Zero disables waiting, so an idle Stream can
    cause repeated empty reads. ``read_page`` has no default wait. After a page,
    the ``read`` iterator's ``next_request`` includes the next request's wait.

    A positive ``rpc_timeout`` caps client and Activity waits below the timeout,
    leaving time for the Store to answer. An Activity that heartbeats between
    Records needs a wait below its heartbeat timeout, or a separate heartbeat task.

    Workflow calls:

    * The SDK selects a Store before each call, including replay. SDK Core handles
      append and read. Create and describe use SDK Local Activities, which pass
      through Worker interceptors.
    * Appends are at-least-once. A Workflow Task retry can write its batch again.
    * Store outages fail the Workflow Task. Workflow code gets no outage error.
    * A missing Store fails the Workflow Task even if Workflow code catches the
      error. A call not awaited before Workflow completion may not run.
    * Workers read the limits at startup. A replacement client must have the same
      limits and Store names.
    * Pages default to 256 Records and 4 MiB, with a 16 MiB byte cap. A live read
      rejects a Record over :py:attr:`max_record_bytes` with
      ``STREAM_ERROR_REASON_UNSUPPORTED``. Reader limits must cover writer limits.
      Replay accepts recorded successes even if the limits decrease.
    * An immediate read can return an empty page. A waiting read stops at its
      wait or the Worker's ``stream_read_time`` limit, whichever comes first.
      At the task limit, ``read_page`` returns an empty page.
    * Iterators can continue across Workflow Tasks. Each page adds a result to
      History, so zero or short waits can cause rapid History growth. The default
      wait limits empty pages, and an idle Stream doesn't create new tasks.
    * A waiting read holds its task and delays Signals until its page or the read
      limit. A server command waits for at most one immediate Store call per read,
      bounded by one second.
    * An unfinished waiting read restarts in the next task. It requests that task
      only when Core knows more Records are available. A task returns at most
      256 read results. Later reads start in the next task.
    * Replay reads the Records from the Store again. Retention must cover the
      Workflow's life and any later replay or query.
    * A body that can't convert to the Stream's type fails the Workflow Task.

    .. warning::
        This API is experimental and subject to change.
    """

    stores: Sequence[StreamStore]
    """Stores available for Stream calls. At least one Store must be provided,
    and each Store must have a unique :py:meth:`StreamStore.name` that isn't
    empty.
    """

    store_selector: Callable[[StreamSelectContext], StreamStore] | None = None
    """Select the Store for a call whose ``StreamRef`` has no Store name.

    Return an instance from :py:attr:`stores`. A selector is required with
    multiple Stores. Without one, the sole Store handles these calls.

    Workflow calls invoke the selector before each command, including creates,
    describes and replay. The selector must return the same Store for the same
    call, must not block or perform I/O, and must be safe across threads. Core
    receives the selected Store name with each command.

    Replayed appends and reads fail with nondeterminism if their selected Store
    differs from History. Create and describe use their recorded results. A
    created Stream's ref includes its Store name, so later calls skip selection.
    """

    max_record_bytes: int = 1536 * 1024
    """Maximum stored Record size for an append or a live Workflow read.

    Includes its Producer. Defaults to 1.5 MiB and must be at least one.
    """

    max_batch_bytes: int = 4 * 1024 * 1024
    """Maximum size in bytes of the Records of one append, each in its stored
    size. Defaults to 4 MiB. Must be greater than or equal to one.
    """

    max_batch_records: int = 1000
    """Maximum number of Records of one append. Defaults to 1000. Must be
    greater than or equal to one.
    """

    _by_name: dict[str, StreamStore] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )
    """Snapshot Store names so later name changes can't change routing."""

    def __post_init__(self) -> None:
        """Validate the Stores and the limits.

        Raises :exc:`ValueError` if no Store is provided, if a limit is less
        than one, if more than one Store is registered without a
        :py:attr:`store_selector`, if a Store has an empty name, or if any two
        Stores have the same name.
        """
        if not self.stores:
            raise ValueError("StreamConfig.stores must contain at least one Store.")
        for limit in ("max_record_bytes", "max_batch_bytes", "max_batch_records"):
            if getattr(self, limit) < 1:
                raise ValueError(
                    f"StreamConfig.{limit} must be greater than or equal to one."
                )
        if len(self.stores) > 1 and self.store_selector is None:
            raise ValueError(
                "StreamConfig.store_selector must be specified if multiple Stores "
                "are registered."
            )
        by_name: dict[str, StreamStore] = {}
        for store in self.stores:
            name = store.name()
            if not name:
                raise ValueError(
                    "StreamConfig.stores contains a Store with an empty name."
                )
            if name in by_name:
                raise ValueError(
                    f"StreamConfig.stores contains multiple Stores with name '{name}'. "
                    "Each Store must have a unique name."
                )
            by_name[name] = store
        object.__setattr__(self, "_by_name", by_name)

    def _store(self, name: str) -> StreamStore:
        store = self._by_name.get(name)
        if store is None:
            raise ValueError(f"No Stream Store found with name '{name}'")
        return store

    def _store_for(
        self,
        ref: sdk.StreamRef,
        target: StorageDriverActivityInfo | StorageDriverWorkflowInfo | None,
        namespace: str,
    ) -> StreamStore:
        if ref.store:
            return self._store(ref.store)
        if self.store_selector is None:
            return self.stores[0]
        selected = self.store_selector(
            StreamSelectContext(id=ref.id, target=target, namespace=namespace)
        )
        if not any(selected is store for store in self.stores):
            raise ValueError(
                "Store returned by store_selector is not registered in StreamConfig.stores"
            )
        return selected

    def _limits(self) -> tuple[int, int, int]:
        return self.max_record_bytes, self.max_batch_bytes, self.max_batch_records


def _request_ref(request: google.protobuf.message.Message) -> sdk.StreamRef:
    if isinstance(request, sdk.CreateStreamRequest):
        return request.stream_ref
    assert isinstance(
        request,
        (sdk.AppendRecordsRequest, sdk.ReadRecordsRequest, sdk.DescribeStreamRequest),
    )
    return request.ref


_Message = type[google.protobuf.message.Message]

_STORE_OPERATIONS: dict[str, tuple[str, _Message, _Message]] = {
    "CreateStream": ("create_stream", CreateStreamRequest, CreateStreamResponse),
    "AppendRecords": ("append_records", AppendRecordsRequest, AppendRecordsResponse),
    "ReadRecords": ("read_records", ReadRecordsRequest, ReadRecordsResponse),
    "DescribeStream": (
        "describe_stream",
        DescribeStreamRequest,
        DescribeStreamResponse,
    ),
}


def _store_operation(name: str) -> tuple[str, _Message, _Message]:
    return _STORE_OPERATIONS[name]


def _sdk_request_type(  # pyright: ignore[reportUnusedFunction]
    name: str,
) -> _Message:
    operation = _STREAM_SERVICE.operation_definitions[name]
    converter = _get_transfer_type_converter(operation.input_type)
    assert converter and converter.transfer_type
    return converter.transfer_type


def _store_request(
    request: google.protobuf.message.Message,
) -> google.protobuf.message.Message:
    stream_id = _request_ref(request).id
    if isinstance(request, sdk.CreateStreamRequest):
        return CreateStreamRequest(
            stream_id=stream_id, id_conflict_policy=request.id_conflict_policy
        )
    if isinstance(request, sdk.AppendRecordsRequest):
        append = AppendRecordsRequest(stream_id=stream_id, records=request.records)
        if request.HasField("producer"):
            append.producer.CopyFrom(request.producer)
        return append
    if isinstance(request, sdk.ReadRecordsRequest):
        read = ReadRecordsRequest(
            stream_id=stream_id,
            from_offset=request.from_offset,
            max_records=request.max_records,
            max_bytes=request.max_bytes,
        )
        # A Store may take a zero wait as one with no end, such as Redis BLOCK 0.
        if request.HasField("wait") and (request.wait.seconds or request.wait.nanos):
            read.wait.CopyFrom(request.wait)
        return read
    return DescribeStreamRequest(stream_id=stream_id)


async def _call_method(
    store: StreamStore,
    operation: str,
    request: google.protobuf.message.Message,
    request_id: str | None,
) -> google.protobuf.message.Message:
    """Translate Store exceptions before Core decides whether to retry."""
    method, _, response_type = _store_operation(operation)
    call = getattr(store, method)
    try:
        if request_id is None:
            response = await call(request)
        else:
            response = await call(request, request_id=request_id)
    except StreamError:
        raise
    except Exception as err:
        raise StreamError(
            f"The Stream Store '{store.name()}' failed the {operation} call "
            f"with {type(err).__name__}: {err}",
            reason=StreamErrorReason.STREAM_ERROR_REASON_STORE_UNAVAILABLE,
        ) from err
    if not isinstance(response, response_type):
        raise StreamError(
            f"The Stream Store '{store.name()}' answered the {operation} call with "
            f"{type(response).__name__}, not with {response_type.__name__}",
            reason=StreamErrorReason.STREAM_ERROR_REASON_STORE_CORRUPT,
        )
    return response


async def _call_store(  # pyright: ignore[reportUnusedFunction]
    store: StreamStore,
    limits: tuple[int, int, int],
    codec: PayloadCodec | None,
    operation: str,
    request: google.protobuf.message.Message,
    request_id: str | None,
    timeout: timedelta | None = None,
) -> google.protobuf.message.Message:
    """Keep client codec and append checks consistent with Workflow calls.

    Records have no serialization context because any client or Workflow can
    read them. Each codec call receives one Record body.
    """
    store_request = _store_request(request)
    if (
        isinstance(store_request, ReadRecordsRequest)
        and store_request.HasField("wait")
        and timeout is not None
        and timeout > timedelta(0)
    ):
        # A wait is only a bound, and a Store that waits all of it must still answer
        # before the timeout fails the read.
        longest = timeout - min(_READ_ANSWER_TIME, timeout / 10)
        if store_request.wait.ToTimedelta() > longest:
            store_request.wait.FromTimedelta(longest)
    if isinstance(store_request, AppendRecordsRequest):
        if codec:
            for appended in store_request.records:
                [body] = await codec.encode([appended.body])
                appended.body.CopyFrom(body)
        # SDK Core checks an append of Workflow code the same way, so each path refuses
        # the same batch before its Store gets it.
        temporalio.bridge.streams.check_append(
            limits, store_request.SerializeToString()
        )
    try:
        # A timeout of zero is no timeout, as for an RPC of the client.
        async with asyncio.timeout(timeout.total_seconds() if timeout else None):
            response = await _call_method(store, operation, store_request, request_id)
    except TimeoutError as err:
        raise StreamError(
            f"The Stream Store '{store.name()}' didn't answer the {operation} call "
            "within its rpc_timeout",
            reason=StreamErrorReason.STREAM_ERROR_REASON_STORE_UNAVAILABLE,
        ) from err
    if isinstance(response, ReadRecordsResponse) and not response.last_offset:
        assert isinstance(store_request, ReadRecordsRequest)
        # Stores may omit the cursor when the page already identifies it.
        records = response.records
        response.last_offset = (
            records[-1].offset if records else store_request.from_offset
        )
    if codec and isinstance(response, ReadRecordsResponse):
        for record in response.records:
            [body] = await codec.decode([record.body])
            record.body.CopyFrom(body)
    if isinstance(response, CreateStreamResponse):
        # Each later call of the created Stream goes to this Store, whatever the
        # selector picks then.
        ref = sdk.StreamRef(id=_request_ref(request).id, store=store.name())
        return sdk.CreateStreamResponse(ref=ref, created=response.created)
    return response


def _activity_target() -> StorageDriverActivityInfo | None:
    if not temporalio.activity.in_activity():
        return None
    info = temporalio.activity.info()
    return StorageDriverActivityInfo(
        id=info.activity_id,
        type=info.activity_type,
        run_id=info.activity_run_id,
        namespace=info.namespace,
    )


async def _call_stream_store(  # pyright: ignore[reportUnusedFunction]
    client: temporalio.client.Client,
    input: temporalio.client.StartNexusOperationInput,
) -> temporalio.client.NexusOperationHandle[Any]:
    stream_config = client._config["stream_config"]
    if stream_config is None:
        raise ValueError(
            "Client has no stream_config, which every Stream call requires."
        )
    data_converter = client.data_converter
    converters = (
        data_converter.payload_converter,
        data_converter.failure_converter,
    )
    request = temporalio.nexus.system._support.model_to_proto(input.arg, *converters)
    store = stream_config._store_for(
        _request_ref(request), _activity_target(), client.namespace
    )
    request_id = None
    if isinstance(request, (sdk.CreateStreamRequest, sdk.AppendRecordsRequest)):
        request_id = str(input._stream_request_id)
    response = await _call_store(
        store,
        stream_config._limits(),
        data_converter.payload_codec,
        input.operation,
        request,
        request_id,
        input.rpc_timeout,
    )
    result_type: Any = input.result_type
    handle: temporalio.client.NexusOperationHandle[Any] = (
        temporalio.client.NexusOperationHandle(
            client,
            input.id,
            result_type=result_type,
            endpoint=input.endpoint,
            service=input.service,
            operation=input.operation,
        )
    )
    handle._known_outcome = temporalio.nexus.system._support.model_from_proto(
        response, result_type, *converters
    )
    return handle


class _StoreCaller:  # pyright: ignore[reportUnusedClass]
    """Keep Store calls on the Python event loop so Core can cancel them."""

    def __init__(self, store: Callable[[], StreamStore]) -> None:
        """Resolve the Store at each call so client replacement takes effect."""
        self._store = store

    async def call(
        self,
        operation: str,
        request: bytes,
        request_id: str | None,
        on_task: Callable[[asyncio.Task[Any]], None],
    ) -> bytes:
        """Register the Python task before any Store work can start.

        ``on_task`` raises :py:class:`asyncio.CancelledError` if Core already
        dropped the call.
        """
        task = asyncio.current_task()
        assert task
        on_task(task)
        _, request_type, _ = _store_operation(operation)
        try:
            response = await _call_method(
                self._store(), operation, request_type.FromString(request), request_id
            )
        except StreamError as err:
            unavailable = StreamErrorReason.STREAM_ERROR_REASON_STORE_UNAVAILABLE
            if err.reason == unavailable and err.__cause__:
                # Core keeps only the text of the error, so the traceback is logged here.
                logger.warning(
                    "Stream Store failed the %s call", operation, exc_info=err.__cause__
                )
            raise
        return response.SerializeToString()
