from __future__ import annotations

import asyncio
from typing import Protocol

from google.protobuf.internal.containers import RepeatedCompositeFieldContainer

from temporalio.api.common.v1.message_pb2 import Payload

PayloadSequence = list[Payload] | RepeatedCompositeFieldContainer[Payload]


class VisitorFunctions(Protocol):
    """Functions invoked by generated payload visitors."""

    async def visit_payload(self, payload: Payload) -> None:
        """Visit a single payload."""
        ...

    async def visit_payloads(self, payloads: PayloadSequence) -> None:
        """Visit a sequence of payloads together."""
        ...

    async def visit_system_nexus_envelope(self, payload: Payload) -> None:
        """Visit a recognized system Nexus envelope payload."""
        return None


class BoundedVisitorFunctions(VisitorFunctions):
    """Wraps VisitorFunctions to cap concurrent payload visits via a semaphore.

    After the full traversal, call drain() to await all in-flight tasks.
    """

    def __init__(self, inner: VisitorFunctions, concurrency_limit: int) -> None:
        """Create a bounded wrapper around the given visitor functions."""
        self._inner = inner
        self._sem = asyncio.Semaphore(concurrency_limit)
        self._tasks: list[asyncio.Task[None]] = []

    async def visit_payload(self, payload: Payload) -> None:
        """Visit a single payload once capacity is available."""
        await self._sem.acquire()

        async def _run() -> None:
            try:
                await self._inner.visit_payload(payload)
            finally:
                self._sem.release()

        self._tasks.append(asyncio.create_task(_run()))

    async def visit_payloads(self, payloads: PayloadSequence) -> None:
        """Visit a sequence of payloads once capacity is available."""
        await self._sem.acquire()

        async def _run() -> None:
            try:
                await self._inner.visit_payloads(payloads)
            finally:
                self._sem.release()

        self._tasks.append(asyncio.create_task(_run()))

    async def visit_system_nexus_envelope(self, payload: Payload) -> None:
        """Visit a system Nexus envelope payload once capacity is available."""
        await self._sem.acquire()

        async def _run() -> None:
            try:
                await self._inner.visit_system_nexus_envelope(payload)
            finally:
                self._sem.release()

        self._tasks.append(asyncio.create_task(_run()))

    async def drain(self) -> None:
        """Wait for all in-flight background tasks to complete.

        On cancellation or error, cancels all remaining tasks and awaits
        them so their finally blocks run before this coroutine returns.
        """
        if not self._tasks:
            return
        try:
            await asyncio.gather(*self._tasks)
        except BaseException:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            raise
