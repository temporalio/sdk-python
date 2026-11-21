"""Stream Store calls through SDK Core. (unstable)

Nothing in this module should be considered stable. The API may change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import temporalio.bridge.runtime
import temporalio.bridge.temporal_sdk_bridge


class StoreCaller(Protocol):
    """What SDK Core calls to reach a Stream Store that is a Python object."""

    async def call(
        self,
        operation: str,
        request: bytes,
        on_task: Callable[[asyncio.Task[Any]], None],
    ) -> bytes:
        """Call the Store for a Stream operation with its encoded request, and
        return its encoded response.
        """
        ...


@dataclass
class WorkerStreams:
    """Python representation of the Rust struct for the Stream Stores of a worker."""

    limits: tuple[int, int, int] | None
    """The ``max_record_bytes``, the ``max_batch_bytes`` and the ``max_batch_records``
    of the Stream config, or ``None`` with no Stream config."""
    stores: Sequence[tuple[str, StoreCaller]]
    """The caller of each Stream Store that Workflow code can use, by Store name."""


async def call_stream_store(
    runtime: temporalio.bridge.runtime.Runtime,
    caller: StoreCaller,
    operation: str,
    request: bytes,
) -> bytes:
    """Call a Stream Store through the Stream runtime of SDK Core, which checks
    the request and the response.
    """
    return await temporalio.bridge.temporal_sdk_bridge.call_stream_store(
        runtime._ref, caller, operation, request
    )


def check_append(limits: tuple[int, int, int], request: bytes) -> None:
    """Check an encoded append as SDK Core checks one of Workflow code.

    ``limits`` are the ``max_record_bytes``, the ``max_batch_bytes`` and the
    ``max_batch_records`` of a Stream config.

    Raises:
        temporalio.exceptions.StreamError: The check refuses the append.
    """
    temporalio.bridge.temporal_sdk_bridge.check_stream_append(limits, request)
