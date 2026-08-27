# pyright: reportUnusedClass=false

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast

from temporalio.contrib.mcp._backend import _MCPBackend, _MCPBackendFactory


class _ConnectionRecord:
    """Own an MCP backend context in one task for its complete lifetime."""

    def __init__(self, factory: Callable[[], _MCPBackend]) -> None:
        loop = asyncio.get_running_loop()
        self._ready: asyncio.Future[_MCPBackend] = loop.create_future()
        self._stop = asyncio.Event()
        self._owner = asyncio.create_task(self._run(factory))
        self._inflight = 0
        self._idle_handle: asyncio.TimerHandle | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._retired = False

    async def _run(self, factory: Callable[[], _MCPBackend]) -> None:
        try:
            backend = factory()
            async with backend:
                self._ready.set_result(backend)
                await self._stop.wait()
        except BaseException as err:
            if not self._ready.done():
                self._ready.set_exception(err)
            else:
                self._failure = err

    async def backend(self) -> _MCPBackend:
        backend = await asyncio.shield(self._ready)
        if self._failure is not None:
            raise self._failure
        return backend

    @property
    def idle(self) -> bool:
        return self._inflight == 0

    @property
    def retired(self) -> bool:
        return self._retired

    def retire(self) -> None:
        self._retired = True

    def acquire(self) -> None:
        self._inflight += 1
        self.cancel_idle()

    def cancel_idle(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def release(
        self, idle_timeout: timedelta | None, on_idle: Callable[[], None]
    ) -> bool:
        self._inflight -= 1
        if self._inflight != 0:
            return False
        if idle_timeout is None:
            return True
        seconds = idle_timeout.total_seconds()
        if seconds == 0:
            on_idle()
        else:
            self._idle_handle = asyncio.get_running_loop().call_later(seconds, on_idle)
        return True

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        self.cancel_idle()
        self._stop.set()
        if not self._ready.done():
            self._owner.cancel()
        try:
            await self._owner
        except BaseException:
            pass
        if self._ready.done() and not self._ready.cancelled():
            self._ready.exception()


class _MCPConnectionPool:
    """Cache parameterless modern MCP connections within each worker event loop."""

    def __init__(
        self,
        factories: dict[str, _MCPBackendFactory],
        idle_timeout: timedelta | None,
    ) -> None:
        if idle_timeout is not None and idle_timeout.total_seconds() < 0:
            raise ValueError("mcp_connection_idle_timeout cannot be negative")
        self._factories = factories
        self._idle_timeout = idle_timeout
        self._records: dict[
            tuple[asyncio.AbstractEventLoop, str], _ConnectionRecord
        ] = {}
        self._locks: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = {}
        self._evictions: set[asyncio.Task[None]] = set()

    def _key(self, server: str) -> tuple[asyncio.AbstractEventLoop, str]:
        return asyncio.get_running_loop(), server

    async def _new_record(self, server: str) -> _ConnectionRecord:
        factory = cast(Callable[[], _MCPBackend], self._factories[server])
        record = _ConnectionRecord(factory)
        try:
            await record.backend()
        except BaseException:
            await record.close()
            raise
        return record

    @asynccontextmanager
    async def backend(
        self,
        server: str,
        *,
        factory_argument: Any,
    ) -> AsyncIterator[_MCPBackend]:
        if factory_argument is not None:
            factory = cast(Callable[[Any], _MCPBackend], self._factories[server])
            async with factory(factory_argument) as backend:
                yield backend
            return

        key = self._key(server)
        lock = self._locks.setdefault(key, asyncio.Lock())
        cached = False
        async with lock:
            record = self._records.get(key)
            if record is None:
                record = await self._new_record(server)
                record_backend = await record.backend()
                if record_backend.cacheable:
                    self._records[key] = record
                    cached = True
            else:
                cached = True
            record.acquire()

        failed = False
        try:
            yield await record.backend()
        except BaseException:
            failed = True
            raise
        finally:
            if failed:
                # Retire the connection so no later operation reuses it, but
                # leave the transport open for operations still in flight on
                # it. The last operation to release the record closes it.
                record.retire()
                await self._unmap(key, record)
            if record.retired:
                if record.release(timedelta(0), lambda: None):
                    await record.close()
            elif cached:
                if self._idle_timeout is None:
                    record.release(None, lambda: None)
                elif self._idle_timeout.total_seconds() == 0:
                    if record.release(self._idle_timeout, lambda: None):
                        await self._evict(key, record)
                else:
                    record.release(
                        self._idle_timeout,
                        lambda: self._schedule_evict(key, record),
                    )
            else:
                record.release(timedelta(0), lambda: None)
                await record.close()

    def _schedule_evict(
        self,
        key: tuple[asyncio.AbstractEventLoop, str],
        record: _ConnectionRecord,
    ) -> None:
        task = asyncio.create_task(self._evict(key, record, only_if_idle=True))
        # The event loop holds only a weak reference to a task, so an unretained
        # eviction can be garbage collected part way through and leave the
        # connection open until the pool closes. Holding it here also lets
        # close() await evictions rather than orphan them on a closing loop.
        self._evictions.add(task)
        task.add_done_callback(self._evictions.discard)

    async def _unmap(
        self,
        key: tuple[asyncio.AbstractEventLoop, str],
        record: _ConnectionRecord,
        *,
        only_if_idle: bool = False,
    ) -> bool:
        """Drop the cached record, returning whether it may now be closed."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # The idle check applies even when the record is no longer mapped:
            # a failing operation unmaps the record while others are still in
            # flight on it, and a pending eviction must not close it early.
            if only_if_idle and not record.idle:
                return False
            if self._records.get(key) is record:
                self._records.pop(key, None)
        return True

    async def _evict(
        self,
        key: tuple[asyncio.AbstractEventLoop, str],
        record: _ConnectionRecord,
        *,
        only_if_idle: bool = False,
    ) -> None:
        if await self._unmap(key, record, only_if_idle=only_if_idle):
            await record.close()

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        records = [
            record
            for (record_loop, _), record in list(self._records.items())
            if record_loop is loop
        ]
        # Disarm timers before yielding so none can create a new eviction after
        # the snapshot below.
        for record in records:
            record.cancel_idle()
        evictions = [task for task in self._evictions if task.get_loop() is loop]
        # An eviction may already have unmapped its record, so let it finish
        # closing that record rather than cancelling it part way through.
        if evictions:
            await asyncio.gather(*evictions, return_exceptions=True)
            self._evictions.difference_update(evictions)
        if records:
            await asyncio.gather(*(record.close() for record in records))
        # Drop the locks along with the records only after eviction tasks have
        # settled; _unmap() creates a lock when an eviction runs.
        for key in list(self._records):
            if key[0] is loop:
                self._records.pop(key, None)
        for key in list(self._locks):
            if key[0] is loop:
                self._locks.pop(key, None)
