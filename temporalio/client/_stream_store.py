from __future__ import annotations

import dataclasses
import enum
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any

import google.protobuf.message
import nexusrpc

import temporalio.activity
import temporalio.api.streamservice.v1
import temporalio.bridge._visitor
import temporalio.common
import temporalio.converter
import temporalio.nexus.system.stream_service
import temporalio.workflow
from temporalio.converter import StorageDriverActivityInfo, StorageDriverWorkflowInfo
from temporalio.exceptions import StreamConfigError

_MAX_RECORD_BYTES = 1536 * 1024
_MAX_BATCH_BYTES = 4 * 1024 * 1024
_MAX_BATCH_RECORDS = 1000


@dataclasses.dataclass(frozen=True)
class StreamBatchLimits:
    """The limits of one batch of Records on a Stream Store.

    The defaults are the largest batch that any Store may take. A Store can allow less, and
    never more.

    .. warning::
        This API is experimental and subject to change.
    """

    max_record_bytes: int = _MAX_RECORD_BYTES
    """The most bytes of one stored Record, from 1 to 1,572,864. The stored form of a Record
    has its Producer and its body."""

    max_batch_bytes: int = _MAX_BATCH_BYTES
    """The most bytes of the stored Records of one batch, from 1 to 4,194,304."""

    max_batch_records: int = _MAX_BATCH_RECORDS
    """The most Records of one batch, from 1 to 1,000."""

    def __post_init__(self) -> None:
        """Check that each limit is from 1 to the default of its field.

        Raises:
            ValueError: A limit is below 1 or above its default.
        """
        for name, most in (
            ("max_record_bytes", _MAX_RECORD_BYTES),
            ("max_batch_bytes", _MAX_BATCH_BYTES),
            ("max_batch_records", _MAX_BATCH_RECORDS),
        ):
            value = getattr(self, name)
            if not 1 <= value <= most:
                raise ValueError(f"{name} must be from 1 to {most}, not {value}")


class StreamReplaySource(enum.Enum):
    """Where a replay of a read of Workflow code gets the Records of the read.

    :py:attr:`StreamConfig.workflow_read_replay_from` holds it. It decides what a new read
    records in History. A replay follows what History holds, so a change of the setting
    doesn't break a Workflow that is running. Client and Activity code have no History, so
    the setting doesn't touch them.

    .. warning::
        This API is experimental and subject to change.
    """

    HISTORY = "history"
    """The marker in History holds the Records of the read with their bodies.

    A replay takes the Records from History, so it reads no Store and needs no Stream
    config. Each Record adds to History. The marker of one Workflow Task holds at most
    1 MiB of Records, and a read goes on in the next task after that.
    """

    STORE = "store"
    """The marker in History holds no Record and no body. This is the default.

    For each Workflow Task of a read, the marker holds the range of offsets, the count
    and a digest of the Records that the Workflow got. A replay reads the Records from the
    Store again and checks the count and the digest. So the replay needs the Stream config
    and the Records. The Records must stay in the Store, and Retention must keep them, for
    as long as the Workflow can replay. When they can't be read, the Workflow Task fails
    with a message that starts with ``StreamReplayError``. A Store that stays unavailable
    for 5 s fails the task with a message that starts with ``StreamReplayUnavailable``
    in its place. A replay on a Worker with no cache reads the Store again on every
    Workflow Task.
    """


@dataclasses.dataclass(frozen=True)
class StreamStoreCallOptions:
    """Options of the Local Activity that creates or describes a Stream for Workflow code.

    Workflow code can't do I/O, so the default
    :py:meth:`StreamStore.call_from_workflow` calls its Store from a Local Activity.
    Core makes a read, an append and a close of Workflow code itself, with no Activity,
    so these options don't apply to them. Core tries such a call again while its Store
    is unavailable, for at most 30 s.

    A :py:class:`StreamConfig` holds them in
    :py:attr:`StreamConfig.workflow_call_options`, and every Store of that config
    gets the same options.

    .. warning::
        This API is experimental and subject to change.
    """

    start_to_close_timeout: timedelta | None = timedelta(seconds=60)
    """Most time for one attempt."""

    schedule_to_close_timeout: timedelta | None = None
    """Most time for all attempts."""

    retry_policy: temporalio.common.RetryPolicy = dataclasses.field(
        default_factory=lambda: temporalio.common.RetryPolicy(maximum_attempts=1)
    )
    """Retries of a failed call. The default is one attempt."""

    def __post_init__(self) -> None:
        """Check that a timeout is set."""
        if not self.start_to_close_timeout and not self.schedule_to_close_timeout:
            raise ValueError(
                "start_to_close_timeout or schedule_to_close_timeout must be set"
            )


class StreamStore(ABC):
    """A Store that answers the Stream operations of a client.

    A client with a Store sends each Stream operation to it. This release has no
    Streams on the server, so a client needs a Store for any Stream operation. Use a
    Store of a package, such as ``RedisStreamStore`` of ``temporalio-streams-redis``, or
    ``MemoryStreamStore`` of ``temporalio-streams-memory`` in tests.

    A Store is a subclass, written in Python, that implements :py:attr:`name` and the
    five operations. It can call its own native code, as the Redis Store does. Each
    operation takes and gives a message of ``temporalio.api.streamservice.v1``. The
    bodies of the Records arrive as payloads that the SDK already encoded with the data
    converter and the payload codec, so a Store keeps payloads and not the user's types.
    A failed call raises :py:class:`temporalio.exceptions.StreamError` with a reason.

    Such a Store works for client, Activity and Workflow code. Core makes a read, an
    append and a close of Workflow code. It calls the Store's method as a task on the
    Worker's event loop, never on a Workflow thread, and records the result in History.
    A create and a describe of Workflow code run in the Store's Local Activity. For
    Workflow code, a Store must do these things.

    * Each method is ``async`` and never blocks the event loop, since the calls of every
      Workflow on the Worker run there.
    * :py:meth:`read_records` waits up to the request's ``wait`` when it has no Record
      after ``from_offset``, and gives an empty page after the wait. An append or a
      close wakes it. Core cancels the task of a read that it stops, for example when
      Workflow code is cancelled or the Worker shuts down, so the wait must end on a
      cancel.
    * Each Record has an offset of 1 to 1024 bytes. Offsets of one Stream compare in
      order as bytes. A page that gives the last Record of a closed Stream is ``done``.
    * :py:meth:`append_records` gives the offsets of the first and the last Record. A
      Workflow appends as a Producer of kind Workflow, with its Workflow Task as the
      epoch. The Store refuses a batch of a lower epoch of that Producer with the reason
      ``STREAM_ERROR_REASON_FENCED``. It writes a batch that comes again with the same
      Producer, epoch and ``sequence`` once, since Core sends it again after an outage.
    * :py:meth:`close_stream` of a closed Stream succeeds, since Core may send a close
      again.
    * A replay of a read under :py:attr:`StreamReplaySource.STORE` reads the Records
      again, so the Store keeps them with the same offsets.
    * An outage raises :py:class:`temporalio.exceptions.StreamError` with the reason
      ``STREAM_ERROR_REASON_STORAGE_UNAVAILABLE``. Core then tries a call of Workflow
      code again for up to 30 s. Every other reason reaches Workflow code on the first
      try. Any other exception, or a ``StreamError`` with no reason, is a fault that a
      retry can't fix. Workflow code gets it on the first try as a non-retryable
      ``STREAM_ERROR_REASON_STORAGE_CORRUPT``, whose message tells the type of the
      exception, and the Worker logs it.

    .. warning::
        This API is experimental and subject to change.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the Store, which the refs of its Streams carry."""
        ...

    @property
    def limits(self) -> StreamBatchLimits:
        """The limits of one batch on this Store.

        The default is the largest batch that any Store may take. A Store that allows less
        overrides this property, and refuses a larger batch itself. Core refuses a larger
        batch of Workflow code before the Store sees it.
        """
        return StreamBatchLimits()

    @abstractmethod
    async def create_stream(
        self, request: temporalio.api.streamservice.v1.CreateStreamRequest
    ) -> temporalio.api.streamservice.v1.CreateStreamResponse:
        """Create a Stream, or get the open one when the conflict policy allows it.

        The ref of the response carries the name of this Store.
        """
        ...

    @abstractmethod
    async def append_records(
        self, request: temporalio.api.streamservice.v1.AppendRecordsRequest
    ) -> temporalio.api.streamservice.v1.AppendRecordsResponse:
        """Append one batch of Records as one Producer.

        A batch that is sent again with the same Producer, epoch and sequence is
        written once.
        """
        ...

    @abstractmethod
    async def read_records(
        self, request: temporalio.api.streamservice.v1.ReadRecordsRequest
    ) -> temporalio.api.streamservice.v1.ReadRecordsResponse:
        """Read one page of Records after an offset.

        At the end of the Stream it waits up to the request's ``wait`` for a new Record.
        """
        ...

    @abstractmethod
    async def close_stream(
        self, request: temporalio.api.streamservice.v1.CloseStreamRequest
    ) -> temporalio.api.streamservice.v1.CloseStreamResponse:
        """Close a Stream. Readers drain what is left, and later appends fail."""
        ...

    @abstractmethod
    async def describe_stream(
        self, request: temporalio.api.streamservice.v1.DescribeStreamRequest
    ) -> temporalio.api.streamservice.v1.DescribeStreamResponse:
        """Give the status and the newest offset of a Stream."""
        ...

    def worker_activities(self) -> Sequence[Callable[..., Any]]:
        """The Activities that a Worker registers for this Store when it runs Workflows.

        The default is one Activity, named for this Store, that
        :py:meth:`call_from_workflow` runs as a Local Activity. A Store that overrides
        one of the two methods overrides the other to match.
        """
        return [_store_call_activity(self)]

    async def call_from_workflow(
        self,
        operation: str,
        request: google.protobuf.message.Message,
        options: StreamStoreCallOptions,
    ) -> google.protobuf.message.Message:
        """Answer a create or a describe of a Stream for Workflow code.

        It runs in Workflow code, so it does no I/O itself. The default runs the
        Activity of :py:meth:`worker_activities` as a Local Activity for each operation.
        It takes the timeouts and the retry policy of ``options``. A replay takes the
        result from History and calls no method of this Store.

        Core makes a read, an append and a close of Workflow code itself, so this method
        gets none of them.

        Args:
            operation: The Nexus operation name, ``CreateStream`` or ``DescribeStream``.
            request: The request message of ``temporalio.api.streamservice.v1``.
            options: The :py:attr:`StreamConfig.workflow_call_options` of the config.

        Returns:
            The response message of the operation.
        """
        stream_operation = _STREAM_OPERATIONS[operation]
        response = await temporalio.workflow.execute_local_activity(
            _store_call_activity_name(self.name),
            args=[operation, request.SerializeToString()],
            result_type=bytes,
            schedule_to_close_timeout=options.schedule_to_close_timeout,
            start_to_close_timeout=options.start_to_close_timeout,
            retry_policy=options.retry_policy,
        )
        return stream_operation.response_type.FromString(response)


@dataclasses.dataclass(frozen=True)
class _StreamOperation:
    """How a Store answers one operation of the Stream service."""

    store_method: str
    """The name of the :py:class:`StreamStore` method that answers it."""

    request_type: type[google.protobuf.message.Message]

    response_type: type[google.protobuf.message.Message]

    async def call(
        self, store: StreamStore, request: google.protobuf.message.Message
    ) -> google.protobuf.message.Message:
        """Call the method of ``store`` for this operation and check its answer."""
        return self.check_response(
            store, await getattr(store, self.store_method)(request)
        )

    def check_response(
        self, store: StreamStore, response: object
    ) -> google.protobuf.message.Message:
        """Give ``response`` back when it has the response type of this operation."""
        # A Store that someone wrote can give another type, and the error names it here
        # rather than in the conversion to the user's type.
        if not isinstance(response, self.response_type):
            raise TypeError(
                f"The Stream Store '{store.name}' answered {self.store_method} with "
                f"{type(response).__name__}, not {self.response_type.__name__}"
            )
        return response


_STREAM_OPERATIONS: dict[str, _StreamOperation] = {
    "CreateStream": _StreamOperation(
        "create_stream",
        temporalio.api.streamservice.v1.CreateStreamRequest,
        temporalio.api.streamservice.v1.CreateStreamResponse,
    ),
    "AppendRecords": _StreamOperation(
        "append_records",
        temporalio.api.streamservice.v1.AppendRecordsRequest,
        temporalio.api.streamservice.v1.AppendRecordsResponse,
    ),
    "ReadRecords": _StreamOperation(
        "read_records",
        temporalio.api.streamservice.v1.ReadRecordsRequest,
        temporalio.api.streamservice.v1.ReadRecordsResponse,
    ),
    "CloseStream": _StreamOperation(
        "close_stream",
        temporalio.api.streamservice.v1.CloseStreamRequest,
        temporalio.api.streamservice.v1.CloseStreamResponse,
    ),
    "DescribeStream": _StreamOperation(
        "describe_stream",
        temporalio.api.streamservice.v1.DescribeStreamRequest,
        temporalio.api.streamservice.v1.DescribeStreamResponse,
    ),
}
"""The operations of the Stream service, by their Nexus operation name."""

_STORE_CALL_ACTIVITY = "__temporal_stream_store_call"


def _store_call_activity_name(store_name: str) -> str:
    # The name carries the Store's name, so two Stores on one Worker don't clash.
    return f"{_STORE_CALL_ACTIVITY}:{store_name}"


def _store_call_activity(
    store: StreamStore,
) -> Callable[[str, bytes], Awaitable[bytes]]:
    @temporalio.activity.defn(name=_store_call_activity_name(store.name))
    async def call(operation: str, request: bytes) -> bytes:
        message = _STREAM_OPERATIONS[operation].request_type.FromString(request)
        response = await _call_stream_store(
            store, temporalio.activity.client().data_converter, operation, message
        )
        return response.SerializeToString()

    return call


@dataclasses.dataclass(frozen=True)
class StreamSelectContext:
    """Context passed to :py:attr:`StreamConfig.store_selector` calls.

    .. warning::
        This API is experimental and subject to change.
    """

    id: str
    """The id of the Stream."""

    target: StorageDriverActivityInfo | StorageDriverWorkflowInfo | None = None
    """The Workflow or Activity that makes the call, or ``None`` for a plain client.

    A call in an Activity has that Activity here. A Workflow's call runs the selector
    in Workflow code, and has that Workflow here.
    """


@dataclasses.dataclass(frozen=True)
class StreamConfig:
    """The Stream Stores of a client, and the rule that picks one for a new Stream.

    A client with this config sends each Stream operation to one of its Stores.
    This release has no Streams on the server, so a client without this config
    raises :py:class:`temporalio.exceptions.StreamConfigError` for a Stream
    operation. It holds Stores and a selector, as
    :py:class:`temporalio.converter.ExternalStorage` holds drivers and a selector.

    On a Worker whose client has no config, a Stream call of Workflow code raises
    :py:class:`temporalio.exceptions.ApplicationError` with the ``type``
    ``StreamConfigError``, and an uncaught one fails the Workflow. Workflow code can
    catch this error. A Workflow that catches it must not go on to make the same call
    on a Worker that has a config, since the History then has no record of the first
    call.

    A ref carries the name of its Store, so a call on a known Stream goes to the
    Store of that name. A create can name its Store with ``store=``. A call whose ref
    names no Store goes to the Store that :py:attr:`store_selector` returns, or to the
    one Store when there is no selector. With more than one Store and no selector,
    such a call raises :py:class:`temporalio.exceptions.StreamConfigError`.

    An append or a close of Workflow code writes as the Workflow, with the Workflow Task
    that runs it as its epoch. So the Store fences a late write of a failed Workflow
    Task. A ``producer=`` of another kind raises
    :py:class:`temporalio.exceptions.StreamError` with the reason
    ``STREAM_ERROR_REASON_INVALID``. Core keeps the appends and the close of one Workflow on one Stream in the order
    of the calls.

    Core appends and closes for Workflow code itself, with no Activity. Core closes the
    Stream only after the server took the Workflow Task of the close, so a failed task
    closes nothing. A create and a describe of Workflow code run in the Store's Local
    Activity. So Workflow code needs no Worker that polls for Activity Tasks.

    Workflow code reads with the same ``read``, ``read_pages`` and ``read_page`` as
    a client. Core reads the Store inside the Workflow Task. A page that Workflow code gets in one activation has at most 256 Records, and
    the rest come in the next activation with no wait. A loop that asks for its next page in the same
    activation keeps one read open in Core. A read that no code waits on closes before
    the activation completes, so it never holds a Workflow Task open.

    .. code-block:: python

        async for record in events.read(wait=timedelta(seconds=10)):
            if record.body.kind == "tool_call":
                tools.append(
                    workflow.start_activity(
                        run_tool,
                        record.body.tool_call,
                        start_to_close_timeout=timedelta(minutes=1),
                    )
                )

    The loop ends after the page that is ``done``, when the Stream is closed and the
    read gave its last Record. With a ``wait``, a quiet Stream gives empty pages, and
    the loop goes on. So a loop on a Stream that can be quiet for long uses
    ``read_pages`` and leaves after an empty page. A read has these costs.

    * A call keeps its Workflow Task open while it waits for its page. A Signal, an
      Update, a cancel or an Activity result reaches the Workflow at the next Workflow
      Task, at the heartbeat of the task at the latest. That is about 8 s with the
      default task timeout of 10 s.
    * The calls of one Workflow Task write one marker in History, whatever the number of
      Records.
    * With the default :py:attr:`StreamReplaySource.STORE`, History holds no Record. A
      replay reads the Records from the Store again, so they must stay in the Store for
      as long as the Workflow can replay.
    * A replay on a Worker with no cache reads the Store again on every Workflow Task.

    Core calls a Store written in Python on the Worker's event loop, with the same rules.
    :py:class:`StreamStore` says what such a Store must do for Workflow code. Code in a
    Query handler or an Update validator can't read.

    .. warning::
        This API is experimental and subject to change.
    """

    stores: Sequence[StreamStore]
    """At least one Store. Each Store has a unique name.

    A ref carries the :py:attr:`StreamStore.name` of its Store, and a call looks
    the Store up by that name. With more than one Store and no
    :py:attr:`store_selector`, each call must name its Store. A Worker serves every
    Store of its client.
    """

    store_selector: Callable[[StreamSelectContext], StreamStore] | None = None
    """Picks the Store of a call whose ref names no Store.

    It gets a :py:class:`StreamSelectContext` for each such call, and returns one of
    the instances in :py:attr:`stores`. When it is ``None`` and there is one Store,
    such a call goes to that Store. When it is ``None`` and there is more than one
    Store, such a call raises :py:class:`temporalio.exceptions.StreamConfigError`.

    For a call of Workflow code, the selector runs in Workflow code, also on a
    replay. So it must give the same Store for the same context. The call of the chosen
    Store is in History, and another answer on a replay is a non-determinism error. A Stream that code builds from an id only goes through the
    selector on each call, so the selector must also give the same Store for the same
    id.
    """

    workflow_call_options: StreamStoreCallOptions = dataclasses.field(
        default_factory=StreamStoreCallOptions
    )
    """Options of a create and a describe of Workflow code, for every Store of this config.

    :py:meth:`StreamStore.call_from_workflow` gets them. The default runs the Store's
    Local Activity with their timeouts and retry policy.
    """

    workflow_read_replay_from: StreamReplaySource = dataclasses.field(
        default=StreamReplaySource.STORE
    )
    """Where a replay of a read of Workflow code gets the Records of the read.

    It applies to every read of Workflow code on a Worker whose client has this config,
    and decides what a new read records in History. :py:attr:`StreamReplaySource.STORE`,
    the default, puts only the range of offsets, the count and a digest of the Records
    into History, so a replay reads them from the Store again. The Records must then stay
    in the Store for as long as the Workflow can replay. The option
    :py:attr:`StreamReplaySource.HISTORY` puts the Records with their bodies into
    History, so a replay needs no Store. A replay follows the marker in History and not
    this setting, so a Workflow that is running replays after a change of it.
    """

    workflow_read_ahead_bytes: int | None = None
    """The most bytes of one read's Records that wait in the Worker for Workflow code.

    A read of Workflow code stops asking the Store while this many bytes of its Records
    wait in the Worker's memory for Workflow code to take them. The read goes
    on when Workflow code takes Records. The first page of a read always comes. ``None``
    takes Core's default of 4 MiB. Set a larger value for large Records, and a smaller one
    for a Worker with little memory, since each open read can hold this much.
    """

    workflow_read_page_records: int | None = None
    """The most Records of one read that Workflow code gets in one activation.

    Core keeps the Records over it and gives them in the next activation, with no wait.
    ``None`` takes Core's default of 256. The value must be 1 to 1000. A replay gives
    each page as History has it, whatever this value is.
    """

    workflow_read_min_gap: timedelta | None = None
    """The least time between two batches of one read of Workflow code.

    A read gives its first batch immediately. The Records that come in the gap after a
    batch come together in the next batch. This trades latency for fewer activations of
    Workflow code, since a Record can wait up to one gap. ``None`` and zero mean no gap,
    the default. The value must be zero to 1 s. A replay ignores it.
    """

    _store_map: dict[str, StreamStore] = dataclasses.field(
        init=False, repr=False, compare=False
    )
    """Name-keyed index of :py:attr:`stores`, built at construction time."""

    def __post_init__(self) -> None:
        """Check the Stores and build the name-keyed map of them.

        Raises :py:class:`temporalio.exceptions.StreamConfigError` if there is no
        Store, or if two Stores share a name. Raises ``ValueError`` if
        :py:attr:`workflow_read_ahead_bytes` is below 1, if
        :py:attr:`workflow_read_page_records` is not 1 to 1000, or if
        :py:attr:`workflow_read_min_gap` is not zero to 1 s.
        """
        if (
            self.workflow_read_ahead_bytes is not None
            and self.workflow_read_ahead_bytes < 1
        ):
            raise ValueError("workflow_read_ahead_bytes must be at least 1")
        if self.workflow_read_page_records is not None and not (
            1 <= self.workflow_read_page_records <= 1000
        ):
            raise ValueError(
                "workflow_read_page_records must be 1 to 1000, "
                f"got {self.workflow_read_page_records}"
            )
        if self.workflow_read_min_gap is not None and not (
            timedelta(0) <= self.workflow_read_min_gap <= timedelta(seconds=1)
        ):
            raise ValueError(
                "workflow_read_min_gap must be zero to 1 second, "
                f"got {self.workflow_read_min_gap}"
            )
        if not self.stores:
            raise StreamConfigError("StreamConfig.stores must have at least one Store")
        store_map: dict[str, StreamStore] = {}
        for store in self.stores:
            if store.name in store_map:
                raise StreamConfigError(
                    f"StreamConfig.stores has more than one Store named '{store.name}'. "
                    "Each Store needs a unique name."
                )
            store_map[store.name] = store
        object.__setattr__(self, "_store_map", store_map)

    def _select_store(self, context: StreamSelectContext) -> StreamStore:
        selector = self.store_selector
        if selector is None:
            if len(self.stores) > 1:
                raise StreamConfigError(
                    "StreamConfig has more than one Store and no store_selector, and "
                    f"the call on the Stream '{context.id}' names no Store. Name one "
                    "with store=, or set StreamConfig.store_selector."
                )
            return self.stores[0]
        store = selector(context)
        if (
            not isinstance(store, StreamStore)
            or self._store_map.get(store.name) is not store
        ):
            raise StreamConfigError(
                f"The store_selector returned {store!r}, which isn't one of "
                "StreamConfig.stores"
            )
        return store

    def _get_store_by_name(self, name: str) -> StreamStore:
        store = self._store_map.get(name)
        if store is None:
            raise StreamConfigError(
                f"The ref names the Stream Store '{name}', which this client doesn't have"
            )
        return store

    def _store_for(
        self,
        ref: temporalio.nexus.system.stream_service.StreamRef[Any],
        target: StorageDriverActivityInfo | StorageDriverWorkflowInfo | None,
    ) -> StreamStore:
        if ref.store:
            return self._get_store_by_name(ref.store)
        return self._select_store(StreamSelectContext(id=ref.id, target=target))


def _request_ref(  # pyright: ignore[reportUnusedFunction]
    request: Any,
) -> temporalio.nexus.system.stream_service.StreamRef[Any]:
    # A create calls its ref `stream_ref` and every other request calls it `ref`, so
    # the field is found by its type.
    if dataclasses.is_dataclass(request) and not isinstance(request, type):
        for field in dataclasses.fields(request):
            value = getattr(request, field.name)
            if isinstance(value, temporalio.nexus.system.stream_service.StreamRef):
                return value
    raise TypeError(f"The request {type(request).__name__} has no StreamRef")


def _activity_target() -> StorageDriverActivityInfo | None:  # pyright: ignore[reportUnusedFunction]
    # External storage builds the same value from the running Activity.
    if not temporalio.activity.in_activity():
        return None
    info = temporalio.activity.info()
    return StorageDriverActivityInfo(
        id=info.activity_id,
        type=info.activity_type,
        run_id=info.activity_run_id,
        namespace=info.namespace,
    )


def _refuse_store_plugins(  # pyright: ignore[reportUnusedFunction]
    plugins: Sequence[object],
) -> None:
    for plugin in plugins:
        if isinstance(plugin, StreamStore):
            raise StreamConfigError(
                f"The Stream Store '{plugin.name}' is in plugins. Give it in "
                "stream_config=StreamConfig(stores=[...]) instead."
            )


def _stream_service() -> nexusrpc.ServiceDefinition:
    service = nexusrpc.get_service_definition(
        temporalio.nexus.system.stream_service.StreamService
    )
    if service is None:
        raise RuntimeError("The generated StreamService has no service definition")
    return service


_STREAM_SERVICE = _stream_service()


async def _call_stream_store(  # pyright: ignore[reportUnusedFunction]
    store: StreamStore,
    data_converter: temporalio.converter.DataConverter,
    operation: str,
    request: google.protobuf.message.Message,
) -> google.protobuf.message.Message:
    stream_operation = _STREAM_OPERATIONS[operation]
    if not data_converter.payload_codec:
        return await stream_operation.call(store, request)
    # The worker package imports the client, so a module level import would cycle.
    from temporalio.bridge.worker import _Visitor

    visitor = temporalio.bridge._visitor.PayloadVisitor(skip_search_attributes=True)
    await visitor.visit(_Visitor(data_converter._encode_payload_sequence), request)
    response = await stream_operation.call(store, request)
    await visitor.visit(_Visitor(data_converter._decode_payload_sequence), response)
    return response
