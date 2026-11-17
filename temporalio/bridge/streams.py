"""The Stream runtime that this bridge build links. (unstable)

Nothing in this module should be considered stable. The API may change.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import temporalio.bridge.runtime
import temporalio.bridge.temporal_sdk_bridge
import temporalio.exceptions

StreamCallError: type[Exception] = temporalio.bridge.temporal_sdk_bridge.StreamCallError


class StreamRuntime:
    """The runtime of the ``temporalio-streams`` crate on one Store."""

    @staticmethod
    def new_memory(
        runtime: temporalio.bridge.runtime.Runtime,
        *,
        name: str,
        retention: timedelta | None,
        producer_idle_ttl: timedelta | None,
    ) -> StreamRuntime:
        """Build the runtime on a new memory Store.

        Raises:
            temporalio.exceptions.StreamConfigError: The bridge build has no Stream
                runtime.
        """
        new = getattr(
            temporalio.bridge.temporal_sdk_bridge, "new_memory_stream_runtime", None
        )
        if new is None:
            raise temporalio.exceptions.StreamConfigError(
                "This build of temporalio has no Stream runtime"
            )
        return StreamRuntime(new(runtime._ref, name, retention, producer_idle_ttl))

    @staticmethod
    def new_python(
        runtime: temporalio.bridge.runtime.Runtime,
        *,
        name: str,
        caller: Any,
    ) -> StreamRuntime:
        """Build the runtime on a Store written in Python, which Core calls for Workflow code.

        ``caller`` runs each call of the Store on the Worker's event loop.

        Raises:
            temporalio.exceptions.StreamConfigError: The bridge build has no Stream
                runtime.
        """
        new = getattr(
            temporalio.bridge.temporal_sdk_bridge, "new_python_stream_runtime", None
        )
        if new is None:
            raise temporalio.exceptions.StreamConfigError(
                "This build of temporalio has no Stream runtime"
            )
        return StreamRuntime(new(runtime._ref, name, caller))

    @staticmethod
    def has_runtime() -> bool:
        """Whether the bridge build has the Stream runtime."""
        return hasattr(
            temporalio.bridge.temporal_sdk_bridge, "new_python_stream_runtime"
        )

    def __init__(self, ref: Any) -> None:
        """Wrap the bridge's runtime object. Use ``new_memory`` or ``new_python`` instead."""
        self._ref = ref

    async def call(self, operation: str, request: bytes) -> bytes:
        """Run one Stream operation on an encoded request and return the encoded response.

        Raises:
            temporalio.exceptions.StreamError: The call failed.
        """
        try:
            return await self._ref.call(operation, request)
        except StreamCallError as err:
            _, message, details = err.args
            raise temporalio.exceptions._stream_error_from_call(
                message, details
            ) from None
