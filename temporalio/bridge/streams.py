"""The Stream runtime that this bridge build links. (unstable)

Nothing in this module should be considered stable. The API may change.
"""

from __future__ import annotations

from collections.abc import Sequence
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
        last_write_ttl: timedelta | None,
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
        return StreamRuntime(new(runtime._ref, name, retention, last_write_ttl))

    @staticmethod
    def new_redis(
        runtime: temporalio.bridge.runtime.Runtime,
        *,
        urls: Sequence[str],
        cluster: bool,
        key_prefix: str | None,
        retention: timedelta | None,
        last_write_ttl: timedelta | None,
        response_timeout: timedelta | None,
        name: str,
        wait_replicas: int,
        wait_timeout: timedelta | None,
    ) -> StreamRuntime:
        """Build the runtime on Redis. It connects at its first call.

        Raises:
            temporalio.exceptions.StreamConfigError: The bridge build has no Redis
                Stream Store.
            ValueError: An option is bad.
        """
        new = getattr(
            temporalio.bridge.temporal_sdk_bridge, "new_redis_stream_runtime", None
        )
        if new is None:
            raise temporalio.exceptions.StreamConfigError(
                "This build of temporalio has no Redis Stream Store"
            )
        return StreamRuntime(
            new(
                runtime._ref,
                list(urls),
                cluster=cluster,
                key_prefix=key_prefix,
                retention=retention,
                last_write_ttl=last_write_ttl,
                response_timeout=response_timeout,
                store_name=name,
                wait_replicas=wait_replicas,
                wait_timeout=wait_timeout,
            )
        )

    def __init__(self, ref: Any) -> None:
        """Wrap the bridge's runtime object. Use ``new_memory`` or ``new_redis`` instead."""
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
