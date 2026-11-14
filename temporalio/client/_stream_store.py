from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import google.protobuf.message
import nexusrpc

import temporalio.activity
import temporalio.api.streamservice.v1
import temporalio.bridge._visitor
import temporalio.bridge.runtime
import temporalio.common
import temporalio.converter
import temporalio.nexus.system.stream_service
import temporalio.runtime
import temporalio.workflow
from temporalio.converter import StorageDriverActivityInfo, StorageDriverWorkflowInfo
from temporalio.exceptions import StreamConfigError

if TYPE_CHECKING:
    import temporalio.bridge.streams


@dataclasses.dataclass(frozen=True)
class StreamStoreCallOptions:
    """Options of the Local Activity that calls a Stream Store for Workflow code.

    Workflow code can't do I/O, so the default
    :py:meth:`StreamStore.call_from_workflow` calls its Store from a Local Activity.

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
    Streams on the server, so a client needs a Store for any Stream operation. Use
    :py:class:`temporalio.contrib.redis.streams.RedisStreamStore`, or
    :py:class:`temporalio.testing.MemoryStreamStore` in tests.

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
        """Answer one Stream operation for Workflow code.

        It runs in Workflow code, so it does no I/O itself. The default runs the
        Activity of :py:meth:`worker_activities` as a Local Activity, with the timeouts
        and the retry policy of ``options``. A replay takes the result from History and
        calls no method of this Store.

        Args:
            operation: The Nexus operation name, ``CreateStream`` or ``DescribeStream``
                in this release.
            request: The request message of ``temporalio.api.streamservice.v1``.
            options: The :py:attr:`StreamConfig.workflow_call_options` of the config.

        Returns:
            The response message of the operation.
        """
        stream_operation = _STREAM_OPERATIONS[operation]
        # Workflow code can start only a create and a describe in this release, and their
        # messages carry no payload. So the Local Activity has no payload codec step.
        response = await temporalio.workflow.execute_local_activity(
            _store_call_activity_name(self.name),
            args=[operation, request.SerializeToString()],
            result_type=bytes,
            schedule_to_close_timeout=options.schedule_to_close_timeout,
            start_to_close_timeout=options.start_to_close_timeout,
            retry_policy=options.retry_policy,
        )
        return stream_operation.response_type.FromString(response)


class _BridgeStreamStore(StreamStore):  # pyright: ignore[reportUnusedClass]
    """A Store that runs on the Stream runtime of the bridge.

    It builds the runtime at its first call, so the Store can be built before the
    Runtime and its Tokio threads exist.
    """

    def __init__(
        self,
        *,
        name: str,
        runtime: temporalio.runtime.Runtime | None,
    ) -> None:
        self._name = name
        self._runtime = runtime
        self._stream_runtime: temporalio.bridge.streams.StreamRuntime | None = None

    @property
    def name(self) -> str:
        """Name of the Store."""
        return self._name

    @abstractmethod
    def _new_stream_runtime(
        self, runtime: temporalio.bridge.runtime.Runtime
    ) -> temporalio.bridge.streams.StreamRuntime: ...

    async def create_stream(
        self, request: temporalio.api.streamservice.v1.CreateStreamRequest
    ) -> temporalio.api.streamservice.v1.CreateStreamResponse:
        """See base class."""
        return await self._call_bridge("CreateStream", request)

    async def append_records(
        self, request: temporalio.api.streamservice.v1.AppendRecordsRequest
    ) -> temporalio.api.streamservice.v1.AppendRecordsResponse:
        """See base class."""
        return await self._call_bridge("AppendRecords", request)

    async def read_records(
        self, request: temporalio.api.streamservice.v1.ReadRecordsRequest
    ) -> temporalio.api.streamservice.v1.ReadRecordsResponse:
        """See base class."""
        return await self._call_bridge("ReadRecords", request)

    async def close_stream(
        self, request: temporalio.api.streamservice.v1.CloseStreamRequest
    ) -> temporalio.api.streamservice.v1.CloseStreamResponse:
        """See base class."""
        return await self._call_bridge("CloseStream", request)

    async def describe_stream(
        self, request: temporalio.api.streamservice.v1.DescribeStreamRequest
    ) -> temporalio.api.streamservice.v1.DescribeStreamResponse:
        """See base class."""
        return await self._call_bridge("DescribeStream", request)

    async def _call_bridge(
        self, operation: str, request: google.protobuf.message.Message
    ) -> Any:
        """Answer one Stream operation with the Stream runtime of the bridge.

        Raises:
            temporalio.exceptions.StreamError: The call failed.
            temporalio.exceptions.StreamConfigError: This build of temporalio has no
                Stream runtime, or not this Store.
        """
        if self._stream_runtime is None:
            runtime = self._runtime or temporalio.runtime.Runtime.default()
            self._stream_runtime = self._new_stream_runtime(runtime._core_runtime)
        # The bridge takes and gives the messages in their wire form.
        response = await self._stream_runtime.call(
            operation, request.SerializeToString()
        )
        return _STREAM_OPERATIONS[operation].response_type.FromString(response)


@dataclasses.dataclass(frozen=True)
class _StreamOperation:
    """How a Store answers one operation of the Stream service."""

    store_method: str
    """The name of the :py:class:`StreamStore` method that answers it."""

    request_type: type[google.protobuf.message.Message]

    response_type: type[google.protobuf.message.Message]

    workflow_refusal: str | None = None
    """Why Workflow code can't start it in this release, or ``None`` when it can."""

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
        workflow_refusal=(
            "Workflow code can't append to a Stream on a Store in this release. "
            "Call a Local Activity that appends through its client."
        ),
    ),
    "ReadRecords": _StreamOperation(
        "read_records",
        temporalio.api.streamservice.v1.ReadRecordsRequest,
        temporalio.api.streamservice.v1.ReadRecordsResponse,
        workflow_refusal=(
            "Workflow code can't read a Stream on a Store in this release. "
            "Call an Activity that reads it through its client."
        ),
    ),
    "CloseStream": _StreamOperation(
        "close_stream",
        temporalio.api.streamservice.v1.CloseStreamRequest,
        temporalio.api.streamservice.v1.CloseStreamResponse,
        workflow_refusal=(
            "Workflow code can't close a Stream on a Store in this release. "
            "Call an Activity that closes it through its client."
        ),
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
        stream_operation = _STREAM_OPERATIONS[operation]
        response = await stream_operation.call(
            store, stream_operation.request_type.FromString(request)
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

    A ref carries the name of its Store, so a call on a known Stream goes to the
    Store of that name. A call whose ref names no Store, such as a create, goes to
    the one Store, or to the Store that :py:attr:`store_selector` returns.

    .. warning::
        This API is experimental and subject to change.
    """

    stores: Sequence[StreamStore]
    """At least one Store. Each Store has a unique name.

    A ref carries the :py:attr:`StreamStore.name` of its Store, and a call looks
    the Store up by that name. With more than one Store, :py:attr:`store_selector`
    must also be set. A Worker serves every Store of its client.
    """

    store_selector: Callable[[StreamSelectContext], StreamStore] | None = None
    """Picks the Store of a new Stream. Required with more than one Store.

    It gets a :py:class:`StreamSelectContext` for each call whose ref names no
    Store, and returns one of the instances in :py:attr:`stores`. When it is
    ``None`` and there is one Store, every new Stream goes to that Store.

    For a call of Workflow code, the selector runs in Workflow code, also on a
    replay. So it must give the same Store for the same context. The Local Activity
    of the chosen Store is in History, and another answer on a replay is a
    non-determinism error. A Stream that code builds from an id only goes through the
    selector on each call, so the selector must also give the same Store for the same
    id.
    """

    workflow_call_options: StreamStoreCallOptions = dataclasses.field(
        default_factory=StreamStoreCallOptions
    )
    """Options of a Store's call for Workflow code, for every Store of this config.

    :py:meth:`StreamStore.call_from_workflow` gets them. The default runs its Local
    Activity with their timeouts and retry policy.
    """

    _store_map: dict[str, StreamStore] = dataclasses.field(
        init=False, repr=False, compare=False
    )
    """Name-keyed index of :py:attr:`stores`, built at construction time."""

    def __post_init__(self) -> None:
        """Check the Stores and build the name-keyed map of them.

        Raises :py:class:`temporalio.exceptions.StreamConfigError` if there is no
        Store, if two Stores share a name, or if more than one Store is given
        without a :py:attr:`store_selector`.
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
        if len(self.stores) > 1 and self.store_selector is None:
            raise StreamConfigError(
                "StreamConfig.store_selector must be set when there is more than one Store"
            )
        object.__setattr__(self, "_store_map", store_map)

    def _select_store(self, context: StreamSelectContext) -> StreamStore:
        selector = self.store_selector
        if selector is None:
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


def _check_not_negative(**durations: timedelta | None) -> None:  # pyright: ignore[reportUnusedFunction]
    for label, value in durations.items():
        if value is not None and value < timedelta():
            raise ValueError(f"{label} must not be negative")


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
