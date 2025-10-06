import asyncio
import inspect
from functools import wraps
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar, Union, cast

from temporalio import activity

F = TypeVar("F", bound=Callable[..., Union[Awaitable[Any], AsyncIterator[Any]]])


def _auto_heartbeater(fn: F) -> F:
    # Propagate type hints from the original callable.
    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        heartbeat_timeout = activity.info().heartbeat_timeout
        heartbeat_task = None
        if heartbeat_timeout:
            # Heartbeat twice as often as the timeout
            heartbeat_task = asyncio.create_task(
                heartbeat_every(heartbeat_timeout.total_seconds() / 2)
            )
        try:
            result = fn(*args, **kwargs)
            # Check if this is a streaming activity (returns async generator)
            if inspect.isasyncgen(result):
                # For streaming activities, yield items while heartbeating
                async def streaming_wrapper():
                    try:
                        async for item in result:
                            yield item
                    finally:
                        if heartbeat_task:
                            heartbeat_task.cancel()
                            try:
                                await heartbeat_task
                            except asyncio.CancelledError:
                                pass

                return streaming_wrapper()
            else:
                # For regular activities, await the result
                # Type narrowing: if it's not an async generator, it must be awaitable
                return await cast(Awaitable[Any], result)
        except Exception:
            # Clean up heartbeat task on error
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            raise

    return cast(F, wrapper)


async def heartbeat_every(delay: float, *details: Any) -> None:
    """Heartbeat every so often while not cancelled"""
    while True:
        await asyncio.sleep(delay)
        activity.heartbeat(*details)
