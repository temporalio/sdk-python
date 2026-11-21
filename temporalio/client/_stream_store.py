from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any

import google.protobuf.message
import nexusrpc

import temporalio.activity
import temporalio.api.streamservice.v1
import temporalio.bridge._visitor
import temporalio.converter
import temporalio.nexus.system.stream_service
from temporalio.converter import StorageDriverActivityInfo
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


class StreamStore(ABC):
    """A Store that answers the Stream operations of a client.

    A client with a Store sends each Stream operation to it. This release has no
    Streams on the server, so a client needs a Store for any Stream operation. Use a
    Store of a package, such as ``RedisStreamStore`` of ``temporalio-streams-redis``, or
    ``MemoryStreamStore`` of ``temporalio-streams-memory`` in tests.

    To keep Streams in another system, write a Store that implements :py:attr:`name`
    and the five operations. Each operation takes and gives a message of
    ``temporalio.api.streamservice.v1``. The bodies of the Records arrive as payloads
    that the SDK already encoded with the data converter and the payload codec, so a
    Store keeps payloads and not the user's types. A failed call raises
    :py:class:`temporalio.exceptions.StreamError` with a reason.

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
        overrides this property, and refuses a larger batch itself.
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


@dataclasses.dataclass(frozen=True)
class StreamSelectContext:
    """Context passed to :py:attr:`StreamConfig.store_selector` calls.

    .. warning::
        This API is experimental and subject to change.
    """

    id: str
    """The id of the Stream."""

    target: StorageDriverActivityInfo | None = None
    """The Activity that makes the call, or ``None`` for a plain client."""


@dataclasses.dataclass(frozen=True)
class StreamConfig:
    """The Stream Stores of a client, and the rule that picks one for a new Stream.

    A client with this config sends each Stream operation to one of its Stores.
    This release has no Streams on the server, so a client without this config
    raises :py:class:`temporalio.exceptions.StreamConfigError` for a Stream
    operation. It holds Stores and a selector, as
    :py:class:`temporalio.converter.ExternalStorage` holds drivers and a selector.

    A ref carries the name of its Store, so a call on a known Stream goes to the
    Store of that name. A create can name its Store with ``store=``. A call whose ref
    names no Store goes to the Store that :py:attr:`store_selector` returns, or to the
    one Store when there is no selector. With more than one Store and no selector,
    such a call raises :py:class:`temporalio.exceptions.StreamConfigError`.

    An Activity reaches the Stores of its Worker's client through
    :py:func:`temporalio.activity.client`. Workflow code can't call a Stream in this
    version. A Stream call of Workflow code raises ``RuntimeError``, which fails the
    Workflow Task.

    .. warning::
        This API is experimental and subject to change.
    """

    stores: Sequence[StreamStore]
    """At least one Store. Each Store has a unique name.

    A ref carries the :py:attr:`StreamStore.name` of its Store, and a call looks
    the Store up by that name. With more than one Store and no
    :py:attr:`store_selector`, each call must name its Store.
    """

    store_selector: Callable[[StreamSelectContext], StreamStore] | None = None
    """Picks the Store of a call whose ref names no Store.

    It gets a :py:class:`StreamSelectContext` for each such call, and returns one of
    the instances in :py:attr:`stores`. When it is ``None`` and there is one Store,
    such a call goes to that Store. When it is ``None`` and there is more than one
    Store, such a call raises :py:class:`temporalio.exceptions.StreamConfigError`.

    A Stream that code builds from an id only goes through the selector on each
    call, so the selector must give the same Store for the same id.
    """

    _store_map: dict[str, StreamStore] = dataclasses.field(
        init=False, repr=False, compare=False
    )
    """Name-keyed index of :py:attr:`stores`, built at construction time."""

    def __post_init__(self) -> None:
        """Check the Stores and build the name-keyed map of them.

        Raises :py:class:`temporalio.exceptions.StreamConfigError` if there is no
        Store, or if two Stores share a name.
        """
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
        target: StorageDriverActivityInfo | None,
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


def _workflow_stream_call_error() -> RuntimeError:  # pyright: ignore[reportUnusedFunction]
    # Not an `ApplicationError`, so it fails the Workflow Task and not the Workflow. A
    # Worker with code that has no Stream call can then finish the Workflow.
    return RuntimeError(
        "Workflow code can't call a Stream in this version. Call it from an Activity "
        "through activity.client()."
    )


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
