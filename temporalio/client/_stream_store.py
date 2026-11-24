"""Stream Stores and the Stream config of a client."""

from __future__ import annotations

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import google.protobuf.message
import nexusrpc

import temporalio.activity
import temporalio.bridge.streams

# pydoctor reads this module before the client package, for the worker modules that
# import it. A `from` import of a sibling here would lose the package's re-exports.
import temporalio.client
import temporalio.nexus.system._support
from temporalio.api.enums.v1 import StreamErrorReason
from temporalio.api.stream.v1 import StreamRef
from temporalio.api.streamservice.v1 import (
    AppendRecordsRequest,
    AppendRecordsResponse,
    CloseStreamRequest,
    CloseStreamResponse,
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


class StreamStore(ABC):
    """Base class of a Stream Store, which keeps the Records of Streams.

    A Store serves the operations of the Nexus service
    ``temporal.api.streamservice.v1.StreamService``. Each method takes the request
    message of its operation and returns its response message, both from
    :py:mod:`temporalio.api.streamservice.v1`. A :py:class:`StreamConfig` registers
    a Store under its :py:meth:`name`, and the SDK sends each Stream call to the
    Store of the call's ``StreamRef``. Only the SDK reads the ``store`` of a ref.

    Callers rely on a Store to follow the rules below and those of each method.
    SDK Core reads a Stream for Workflow code and reads it again in a replay, and
    both depend on them.

    The log:

    * A Store keeps the body of a Record as the append gave it. The payload codec
      has already encoded it.
    * A Store writes a batch, which is the Records of one append, whole or not at
      all. Its Records are next to each other, in the order of the request.
    * The offsets of a Stream sort bytewise in the order of its Records, and the
      offset of a Record is never empty.
    * Once a response returned an offset, no Record ever appears at or below it
      later, even from an append that ran at the same time as that call.
    * A Store doesn't change or remove a Record before its Stream expires.

    Producers and attempts:

    * A Producer is one writer of a Stream, which is a Workflow run, an Activity,
      a Local Activity or a client. Two Producers are the same when they have the
      same kind and the same ids of that kind.
    * An attempt is the try of a Producer that made a write, such as one Workflow
      Task or one attempt of an Activity.
    * Fencing is optional. A Store that fences keeps the newest attempt of each
      Producer that appended to a Stream, and refuses an append or a close of an
      earlier attempt of that Producer. Only an append changes the newest attempt.
      Every Record has its Producer and attempt either way, so a reader can tell
      the attempts apart.

    A method refuses a call by raising a
    :py:class:`temporalio.exceptions.StreamError` with the reason, such as
    ``STREAM_ERROR_REASON_CLOSED`` for ``CLOSED`` in these docs. Any other
    exception is an outage of the Store, and the caller gets a ``StreamError``
    with ``STREAM_ERROR_REASON_STORE_UNAVAILABLE``.

    How the SDK calls a Store:

    * Calls run at the same time on the event loop, and a read can wait while
      appends run.
    * The SDK can cancel the task of a call, for example at a timeout, at an
      eviction or at a Worker shutdown. The call may still have taken effect, and
      the Store must stay usable after it.
    * A resend can come while its first try still runs, and the Store still writes
      the batch once.
    * SDK Core checks each append, close and read of Workflow code, and its
      response, and tries the call again during an outage. For every other call
      the SDK checks only an append, against the limits of the
      :py:class:`StreamConfig`, and sends one request. A Store that needs more
      checks runs them itself.

    .. warning::
        This API is experimental and subject to change.
    """

    @abstractmethod
    def name(self) -> str:
        """Return the name that a :py:class:`StreamConfig` registers this Store
        under. A Store may take its name as an argument, so that two instances of
        one Store class can be in one config.
        """
        raise NotImplementedError

    @abstractmethod
    async def create_stream(
        self, request: CreateStreamRequest, *, request_id: str
    ) -> CreateStreamResponse:
        """Create a Stream, or return the open one if the conflict policy allows it.

        Each try of one create has the same ``request_id``. A create with the
        ``request_id`` of the create that made the Stream is a retry of it, and a
        create with an empty ``request_id`` never is. The response has the ref as
        the request had it.

        A Store checks a create for an id that has a Stream in this order:

        1. The Stream expired, so the create fails with ``EXPIRED``.
        2. The Stream is closed, so the create fails with ``CLOSED``.
        3. The create is a retry, so it returns ``created`` true.
        4. The policy is ``USE_EXISTING``, so it returns ``created`` false. With
           ``FAIL``, or with no policy, it fails with ``ALREADY_EXISTS``.
        """
        raise NotImplementedError

    @abstractmethod
    async def append_records(
        self, request: AppendRecordsRequest, *, request_id: str
    ) -> AppendRecordsResponse:
        """Append one batch from one Producer.

        Each try of one append has the same ``request_id``. A Store remembers the
        request ids of the recent batches of each Producer of a Stream, at least
        the last one. An append with the Producer, the attempt and the
        ``request_id`` of a remembered batch is a resend. An append with an empty
        ``request_id`` never is.

        A Store checks an append in this order:

        1. The Stream doesn't exist or it expired, so the append fails with
           ``NOT_FOUND`` or ``EXPIRED``.
        2. The append is a resend, so the Store writes nothing and returns the
           offsets of the first write. It doesn't compare the Records, and the
           Stream can be closed by then.
        3. The Store fences, and a later attempt of the Producer already wrote, so
           the append fails with ``FENCED``.
        4. The Stream is closed, so the append fails with ``CLOSED``.

        The response has the offsets of the first and the last Record.
        """
        raise NotImplementedError

    @abstractmethod
    async def read_records(self, request: ReadRecordsRequest) -> ReadRecordsResponse:
        """Read one page of Records after ``from_offset``, in the order of the Stream.

        * A page has at most ``max_records`` Records, and Records of at most
          ``max_bytes`` in total, where zero is no limit. It always has its first
          Record, even when that Record alone is over ``max_bytes``.
        * ``last_offset`` is where the next read continues. A Store doesn't have to
          set it. If it's empty, the caller gets the offset of the last Record of
          the page, or ``from_offset`` for a page with no Record.
        * A page is ``done`` when the Stream is closed and no Record comes after
          the page.
        * A read with nothing after ``from_offset`` answers once a Record comes,
          once the Stream is closed, or once its ``wait`` is up.
        * Every offset that a response returned is a valid ``from_offset``. A read
          fails with ``OFFSET_TOO_HIGH`` only when ``from_offset`` is past the end
          of the Stream.
        """
        raise NotImplementedError

    @abstractmethod
    async def close_stream(self, request: CloseStreamRequest) -> CloseStreamResponse:
        """Close a Stream.

        A Store checks a close in this order:

        1. The Stream doesn't exist or it expired, so the close fails with
           ``NOT_FOUND`` or ``EXPIRED``.
        2. The Stream is already closed, so the close succeeds.
        3. The Store fences, and a later attempt of the Producer already wrote, so
           the close fails with ``FENCED``.
        """
        raise NotImplementedError

    @abstractmethod
    async def describe_stream(
        self, request: DescribeStreamRequest
    ) -> DescribeStreamResponse:
        """Return the status of a Stream, and a ``last_offset`` that is never below
        the offset of its newest Record.
        """
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
    """The Stream Stores of a client and of each Worker that uses the client.

    The SDK checks each append against :py:attr:`max_record_bytes`,
    :py:attr:`max_batch_bytes` and :py:attr:`max_batch_records` before its Store
    gets it, and refuses one over a limit with
    ``STREAM_ERROR_REASON_TOO_LARGE``. The ``rpc_metadata`` of a Stream call has
    no effect, since a call to a Store is not an RPC of the Temporal server.

    For the Stream calls of Workflow code:

    - SDK Core makes each append, close and read. A create and a describe run in
      a Local Activity of the SDK, which the interceptors of the Worker see as
      any other Local Activity.
    - An append is at-least-once. If a Workflow Task fails after its append, the
      next attempt of the task writes the batch again.
    - An outage of the Store gives Workflow code no error. The Workflow Task
      fails, and the server tries it again.
    - A call that Workflow code doesn't await before the Workflow ends may not
      happen. A Workflow that needs its close awaits it, as ``async with`` does.
    - A call with no Store fails the Workflow Task, also when Workflow code
      catches the error.
    - A Worker reads the three limits when it starts. It refuses a new client
      with other limits, as it refuses one with other Store names.
    - A page of a read has at most 256 Records and 4 MiB of Records by default,
      and never more than 16 MiB.
    - A read fails with ``STREAM_ERROR_REASON_UNSUPPORTED`` for a Record over
      :py:attr:`max_record_bytes`. So a reader needs a limit at least as large
      as that of each writer of its Stream.
    - A read that waits holds its Workflow Task, up to the read limit of the
      task, which the Worker option ``stream_read_time`` sets. A Signal that
      comes meanwhile waits until the read has its page, or until that limit. At
      the limit, a read with no answer gives a page with no Record. A command
      for the server doesn't wait for a read.
    - A body that isn't of the Stream's type fails the Workflow Task, as an
      Activity result that can't be converted does.

    .. warning::
        This API is experimental and subject to change.
    """

    stores: Sequence[StreamStore]
    """Stores available for Stream calls. At least one Store must be provided,
    and each Store must have a unique :py:meth:`StreamStore.name` that isn't
    empty.
    """

    store_selector: Callable[[StreamSelectContext], StreamStore] | None = None
    """Picks the Store of a call whose ``StreamRef`` has no Store name. It must
    return one of :py:attr:`stores`.

    Required when more than one Store is registered. When ``None``, the one
    Store takes each such call.

    A Workflow replay calls the selector again, so it must return the same Store
    for the same call. A created Stream has the name of its Store in its ref, so
    its calls don't depend on the selector.
    """

    max_record_bytes: int = 1536 * 1024
    """Maximum size in bytes of one Record of an append, and of one Record that a
    read of Workflow code gives. Defaults to 1.5 MiB. Must be greater than or
    equal to one. The size is that of the stored Record, which counts its
    Producer.
    """

    max_batch_bytes: int = 4 * 1024 * 1024
    """Maximum size in bytes of the Records of one append, each in its stored
    size. Defaults to 4 MiB. Must be greater than or equal to one.
    """

    max_batch_records: int = 1000
    """Maximum number of Records of one append. Defaults to 1000. Must be
    greater than or equal to one.
    """

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
        names: set[str] = set()
        for store in self.stores:
            name = store.name()
            if not name:
                raise ValueError(
                    "StreamConfig.stores contains a Store with an empty name."
                )
            if name in names:
                raise ValueError(
                    f"StreamConfig.stores contains multiple Stores with name '{name}'. "
                    "Each Store must have a unique name."
                )
            names.add(name)

    def _store(self, name: str) -> StreamStore:
        """Return the Store with a name, raising :class:`ValueError` if none has it."""
        for store in self.stores:
            if store.name() == name:
                return store
        raise ValueError(f"No Stream Store found with name '{name}'")

    def _store_for(
        self,
        ref: StreamRef,
        target: StorageDriverActivityInfo | StorageDriverWorkflowInfo | None,
        namespace: str,
    ) -> StreamStore:
        """Return the Store of a call on ``ref``, the ref of the call's request,
        raising :class:`ValueError` if there is none.
        """
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
        """Return the three limits, in the order in which the bridge takes them."""
        return self.max_record_bytes, self.max_batch_bytes, self.max_batch_records


def _request_ref(request: google.protobuf.message.Message) -> StreamRef:
    """Return the ref of a Stream request, which is ``stream_ref`` in a create."""
    if isinstance(request, CreateStreamRequest):
        return request.stream_ref
    assert isinstance(
        request,
        (
            AppendRecordsRequest,
            ReadRecordsRequest,
            CloseStreamRequest,
            DescribeStreamRequest,
        ),
    )
    return request.ref


def _operation(
    name: str,
) -> tuple[
    str, type[google.protobuf.message.Message], type[google.protobuf.message.Message]
]:
    """Return the :py:class:`StreamStore` method of a Stream operation, with the
    classes of its request message and of its response message.
    """
    operation = _STREAM_SERVICE.operation_definitions[name]
    request, response = (
        _get_transfer_type_converter(model)
        for model in (operation.input_type, operation.output_type)
    )
    assert request and request.transfer_type and response and response.transfer_type
    return operation.method_name, request.transfer_type, response.transfer_type


async def _call_method(
    store: StreamStore,
    operation: str,
    request: google.protobuf.message.Message,
    request_id: str | None,
) -> google.protobuf.message.Message:
    """Call the method of ``store`` for a Stream operation. ``request_id`` is that
    of a create or an append, and ``None`` for the other operations.

    Raises:
        StreamError: The Store refused the call. The reason is
            ``STREAM_ERROR_REASON_STORE_UNAVAILABLE`` if the method raised any
            other exception, and ``STREAM_ERROR_REASON_STORE_CORRUPT`` if it
            returned something else than the response message of the operation.
    """
    method, _, response_type = _operation(operation)
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
    """Call a Store for client code, which is :py:func:`_call_method` with the
    payload codec, the check of an append, the Store of a created Stream and a
    timeout.

    The codec gets each body of an append and each Record of a read alone, with no
    serialization context, since the reader of a Record can be any code. ``limits``
    are those of the Stream config, from :py:meth:`StreamConfig._limits`. The ref
    of a create's response gets the name of ``store``.

    Raises:
        StreamError: The call failed. The reason is
            ``STREAM_ERROR_REASON_STORE_CORRUPT`` for a create's response whose ref
            doesn't have the Stream id of the request.
    """
    if codec and isinstance(request, AppendRecordsRequest):
        for appended in request.records:
            [body] = await codec.encode([appended.body])
            appended.body.CopyFrom(body)
    if isinstance(request, AppendRecordsRequest):
        # SDK Core checks an append of Workflow code the same way, so each path refuses
        # the same batch before its Store gets it.
        temporalio.bridge.streams.check_append(limits, request.SerializeToString())
    try:
        # A timeout of zero is no timeout, as for an RPC of the client.
        async with asyncio.timeout(timeout.total_seconds() if timeout else None):
            response = await _call_method(store, operation, request, request_id)
    except TimeoutError as err:
        raise StreamError(
            f"The Stream Store '{store.name()}' didn't answer the {operation} call "
            "within its rpc_timeout",
            reason=StreamErrorReason.STREAM_ERROR_REASON_STORE_UNAVAILABLE,
        ) from err
    if isinstance(response, ReadRecordsResponse) and not response.last_offset:
        assert isinstance(request, ReadRecordsRequest)
        # The page shows where it ended, so a Store doesn't have to say it.
        records = response.records
        response.last_offset = records[-1].offset if records else request.from_offset
    if codec and isinstance(response, ReadRecordsResponse):
        for record in response.records:
            [body] = await codec.decode([record.body])
            record.body.CopyFrom(body)
    if isinstance(response, CreateStreamResponse):
        assert isinstance(request, CreateStreamRequest)
        if response.ref.id != request.stream_ref.id:
            raise StreamError(
                f"The Stream Store '{store.name()}' answered the {operation} call "
                "without the Stream id of the request",
                reason=StreamErrorReason.STREAM_ERROR_REASON_STORE_CORRUPT,
            )
        # Each later call of the created Stream goes to this Store, whatever the
        # selector picks then.
        response.ref.store = store.name()
    return response


def _activity_target() -> StorageDriverActivityInfo | None:
    """Return the running Activity as the target of a Stream call, if there is one."""
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
    """Call the Stream Store of a Stream operation of a client, and return a handle
    that has its result.
    """
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
    if isinstance(request, (CreateStreamRequest, AppendRecordsRequest)):
        request_id = str(uuid.uuid4())
    response = await _call_store(
        store,
        stream_config._limits(),
        data_converter.payload_codec,
        input.operation,
        request,
        request_id,
        input.rpc_timeout,
    )
    # Each generated Stream call gives its result type.
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
    """The caller of a Stream Store for the bridge, which gives and takes encoded
    messages.

    SDK Core calls :py:meth:`call` for each append, close and read of Workflow
    code.
    """

    def __init__(self, store: Callable[[], StreamStore]) -> None:
        """Create a caller of the Store that ``store`` returns at each call."""
        self._store = store

    async def call(
        self,
        operation: str,
        request: bytes,
        request_id: str | None,
        on_task: Callable[[asyncio.Task[Any]], None],
    ) -> bytes:
        """Call the Store for a Stream operation and return its encoded response.

        ``request_id`` is that of a create or an append, and ``None`` for the other
        operations. ``on_task`` gets the task of this call, so that the bridge can
        cancel it. It raises :py:class:`asyncio.CancelledError` if the bridge has
        already dropped the call.
        """
        task = asyncio.current_task()
        assert task
        on_task(task)
        _, request_type, _ = _operation(operation)
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
