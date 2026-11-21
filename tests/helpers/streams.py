"""Helpers for the tests of Streams."""

import asyncio
import collections
import selectors
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

from google.protobuf.message import Message

from temporalio.api.common.v1 import Payload
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
from temporalio.client import StreamStore
from temporalio.converter import (
    PayloadCodec,
    SerializationContext,
    WithSerializationContext,
)
from temporalio.testing import MemoryStreamStore

_T = TypeVar("_T")

CodecCall = tuple[str, SerializationContext | None, int]


class ContextCodec(PayloadCodec, WithSerializationContext):
    """Wraps each payload with the kind of its serialization context as a key, and
    refuses to unwrap a payload with another key. ``calls`` logs the context and the
    payload count of each call.
    """

    def __init__(
        self,
        calls: list[CodecCall] | None = None,
        context: SerializationContext | None = None,
    ) -> None:
        self.calls: list[CodecCall] = [] if calls is None else calls
        self._context = context

    def with_context(self, context: SerializationContext) -> "ContextCodec":
        return ContextCodec(self.calls, context)

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        self.calls.append(("encode", self._context, len(payloads)))
        return [wrapped(payload, self._context) for payload in payloads]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        self.calls.append(("decode", self._context, len(payloads)))
        decoded: list[Payload] = []
        for payload in payloads:
            if payload.metadata.get("encoding") == b"test/wrapped":
                key = wrapped(Payload(), self._context).metadata["context"]
                assert payload.metadata["context"] == key, (
                    f"{payload} isn't for {key!r}"
                )
                payload = Payload.FromString(payload.data)
            decoded.append(payload)
        return decoded


def wrapped(payload: Payload, context: SerializationContext | None = None) -> Payload:
    """Return ``payload`` as a :py:class:`ContextCodec` with ``context`` encodes it."""
    return Payload(
        metadata={
            "encoding": b"test/wrapped",
            "context": type(context).__name__.encode(),
        },
        data=payload.SerializeToString(),
    )


Fault = Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]]
"""What a :py:class:`FaultStore` does with one call. It gets the call of the memory Store."""


@contextmanager
def failure_note(note: str) -> Iterator[None]:
    """Add ``note`` to a failure in the block, such as the seed of a generated case."""
    try:
        yield
    except BaseException as err:
        err.add_note(note)
        raise


def raises(error: BaseException) -> Fault:
    """Return a fault that fails a call, and doesn't call the memory Store."""

    async def fault(_call: Callable[[], Awaitable[Any]]) -> Any:
        raise error

    return fault


def answers(response: Message) -> Fault:
    """Return a fault that answers a call with ``response``, with no Store call."""

    async def fault(_call: Callable[[], Awaitable[Any]]) -> Any:
        return response

    return fault


def loses_answer() -> Fault:
    """Return a fault that calls the memory Store and then fails as an outage."""

    async def fault(call: Callable[[], Awaitable[Any]]) -> Any:
        await call()
        raise ConnectionError("The answer of the Store was lost")

    return fault


class Hold:
    """A fault that holds a call until :py:meth:`release`, before the memory Store."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self._released = asyncio.Event()

    def release(self) -> None:
        self._released.set()

    async def __call__(self, call: Callable[[], Awaitable[Any]]) -> Any:
        self.started.set()
        try:
            await self._released.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return await call()


class FaultStore(StreamStore):
    """Stream Store around a :py:class:`MemoryStreamStore`, which logs each request
    and runs the next fault of its method, if one is queued.
    """

    def __init__(self, name: str = "memory", **options: Any) -> None:
        self.memory = MemoryStreamStore(name=name, **options)
        self.requests: list[Message] = []
        self.faults: dict[str, collections.deque[Fault]] = collections.defaultdict(
            collections.deque
        )

    def name(self) -> str:
        return self.memory.name()

    async def create_stream(self, request: CreateStreamRequest) -> CreateStreamResponse:
        return await self._call("create_stream", request)

    async def append_records(
        self, request: AppendRecordsRequest
    ) -> AppendRecordsResponse:
        return await self._call("append_records", request)

    async def read_records(self, request: ReadRecordsRequest) -> ReadRecordsResponse:
        return await self._call("read_records", request)

    async def close_stream(self, request: CloseStreamRequest) -> CloseStreamResponse:
        return await self._call("close_stream", request)

    async def describe_stream(
        self, request: DescribeStreamRequest
    ) -> DescribeStreamResponse:
        return await self._call("describe_stream", request)

    async def _call(self, method: str, request: Message) -> Any:
        self.requests.append(request)
        call = lambda: getattr(self.memory, method)(request)
        if self.faults[method]:
            return await self.faults[method].popleft()(call)
        return await call()


class _JumpingSelector(selectors.DefaultSelector):
    """Selector that moves the clock of its loop past a wait and doesn't wait."""

    def __init__(self, loop: "VirtualClockLoop") -> None:
        super().__init__()
        self._loop = loop

    def select(
        self, timeout: float | None = None
    ) -> list[tuple[selectors.SelectorKey, int]]:
        # With no timer, only another thread can wake the loop, so this waits for it.
        if timeout is None:
            return super().select(None)
        events = super().select(0)
        if not events:
            self._loop.now += max(timeout, 0)
        return events


class VirtualClockLoop(asyncio.SelectorEventLoop):
    """Event loop whose clock jumps to its next timer when no task can run.

    A test on it must not wait for another thread while a timer is pending, since
    the clock then jumps past the wait.
    """

    def __init__(self) -> None:
        self.now = 0.0
        super().__init__(_JumpingSelector(self))
        # asyncio runs a timer that is due within the resolution of the real clock, which is
        # about 16 ms on Windows. This clock is exact, so its timers run when they are due.
        self._clock_resolution = 1e-9

    def time(self) -> float:
        return self.now


def run_with_virtual_clock(main: Coroutine[Any, Any, _T]) -> _T:
    """Run ``main`` on a new :py:class:`VirtualClockLoop` and return its result."""
    loop = VirtualClockLoop()
    try:
        return loop.run_until_complete(main)
    finally:
        loop.close()
