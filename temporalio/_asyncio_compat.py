"""Backports for asyncio functionality for older Python versions."""

from __future__ import annotations

import asyncio
import sys
from typing import Optional

# Only define these if we're on Python < 3.11
if sys.version_info < (3, 11):

    class BrokenBarrierError(Exception):
        """Exception raised when a Barrier is broken."""
        pass

    class Barrier:
        """Backport of asyncio.Barrier for Python < 3.11.
        
        A barrier is a synchronization primitive that allows a set number of tasks
        to wait until they have all reached the barrier before proceeding.
        """

        def __init__(self, parties: int) -> None:
            """Initialize a barrier.

            Args:
                parties: The number of tasks that must call wait() before any
                    of them can proceed.

            Raises:
                ValueError: If parties is less than 1.
            """
            if parties < 1:
                raise ValueError("parties must be greater than 0")
            
            self._parties = parties
            self._count = 0
            self._broken = False
            self._waiters: list[asyncio.Future[int]] = []
            self._state_lock = asyncio.Lock()
            
        async def wait(self) -> int:
            """Wait for all parties to reach the barrier.

            Returns:
                The index of the current task (0 to parties-1).

            Raises:
                BrokenBarrierError: If the barrier is broken or gets broken
                    while waiting.
            """
            async with self._state_lock:
                if self._broken:
                    raise BrokenBarrierError("Barrier is broken")
                
                index = self._count
                self._count += 1
                
                if self._count == self._parties:
                    # We're the last one, release everyone
                    self._count = 0
                    for i, waiter in enumerate(self._waiters):
                        if not waiter.done():
                            waiter.set_result(i)
                    self._waiters.clear()
                    return index
                else:
                    # We need to wait
                    fut = asyncio.get_running_loop().create_future()
                    self._waiters.append(fut)
            
            try:
                return await fut
            except asyncio.CancelledError:
                # If we're cancelled, we need to handle cleanup
                async with self._state_lock:
                    # Remove our future from waiters if it's still there
                    try:
                        self._waiters.remove(fut)
                        self._count -= 1
                    except ValueError:
                        # Future was already processed
                        pass
                    
                    # If we were the last waiter and got cancelled, break the barrier
                    if self._count == 0 and not self._broken:
                        self._broken = True
                        self._break_barrier()
                raise

        def reset(self) -> None:
            """Reset the barrier to its initial state.

            Any tasks currently waiting will receive BrokenBarrierError.
            """
            # Note: This is synchronous in Python 3.11+, so we keep it synchronous
            # but need to handle the async context properly
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule the reset to run in the event loop
                asyncio.create_task(self._reset_async())
            else:
                # If no loop is running, we can't really do much
                # This matches the behavior where reset() expects to be called
                # from within an async context
                raise RuntimeError("Cannot reset barrier outside of async context")

        async def _reset_async(self) -> None:
            """Async implementation of reset."""
            async with self._state_lock:
                self._count = 0
                self._broken = False
                self._break_barrier()
                self._waiters.clear()

        def abort(self) -> None:
            """Place the barrier into a broken state.

            This causes any current or future calls to wait() to fail with
            BrokenBarrierError.
            """
            # Note: This is synchronous in Python 3.11+, so we keep it synchronous
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule the abort to run in the event loop
                asyncio.create_task(self._abort_async())
            else:
                raise RuntimeError("Cannot abort barrier outside of async context")

        async def _abort_async(self) -> None:
            """Async implementation of abort."""
            async with self._state_lock:
                self._broken = True
                self._break_barrier()

        def _break_barrier(self) -> None:
            """Break the barrier, causing all waiters to get BrokenBarrierError.
            
            Must be called while holding the state lock.
            """
            for waiter in self._waiters:
                if not waiter.done():
                    waiter.set_exception(BrokenBarrierError("Barrier is broken"))

        @property
        def parties(self) -> int:
            """Return the number of parties required to pass the barrier."""
            return self._parties

        @property
        def n_waiting(self) -> int:
            """Return the number of tasks currently waiting at the barrier."""
            return self._count

        @property
        def broken(self) -> bool:
            """Return True if the barrier is in a broken state."""
            return self._broken

else:
    # Python 3.11+, use the built-in
    from asyncio import Barrier, BrokenBarrierError  # type: ignore[attr-defined]

__all__ = ["Barrier", "BrokenBarrierError"]
