"""A Stream Store in the memory of the process, for tests."""

from __future__ import annotations

from datetime import timedelta

import temporalio.bridge.runtime
import temporalio.bridge.streams
import temporalio.client._stream_store
import temporalio.runtime


class MemoryStreamStore(temporalio.client._stream_store._BridgeStreamStore):
    """A Stream Store that keeps its Streams in the memory of this process, for tests.

    It runs the same Stream runtime and rules as every other Store on the bridge. A
    failed call raises :py:class:`temporalio.exceptions.StreamError`.

    .. warning::
        This API is experimental and subject to change.
    """

    def __init__(
        self,
        *,
        name: str = "memory",
        retention: timedelta | None = None,
        last_write_ttl: timedelta | None = None,
        runtime: temporalio.runtime.Runtime | None = None,
    ) -> None:
        """Create a memory Store. It builds its Stream runtime at its first call.

        Args:
            name: Name of the Store, which the refs of its Streams carry.
            retention: How long a closed Stream stays. ``None`` takes the default of
                the Stream runtime.
            last_write_ttl: How long an open Stream stays after its last write.
                ``None`` keeps an open Stream until its close.
            runtime: Runtime whose Tokio threads run the Store. ``None`` takes
                :py:meth:`temporalio.runtime.Runtime.default` at the first call.
        """
        temporalio.client._stream_store._check_not_negative(
            retention=retention, last_write_ttl=last_write_ttl
        )
        super().__init__(name=name, runtime=runtime)
        self._retention = retention
        self._last_write_ttl = last_write_ttl

    def _new_stream_runtime(
        self, runtime: temporalio.bridge.runtime.Runtime
    ) -> temporalio.bridge.streams.StreamRuntime:
        return temporalio.bridge.streams.StreamRuntime.new_memory(
            runtime,
            name=self._name,
            retention=self._retention,
            last_write_ttl=self._last_write_ttl,
        )
