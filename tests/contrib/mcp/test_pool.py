import asyncio
from datetime import timedelta
from typing import Any, cast

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from temporalio.contrib.mcp._client import _MCPClientBackend
from temporalio.contrib.mcp._pool import _MCPConnectionPool


def echo_server() -> MCPServer[Any]:
    server = MCPServer("echo")

    @server.tool()
    def echo(value: str) -> str:  # type: ignore[reportUnusedFunction]
        return value

    return server


async def test_modern_connection_is_reused() -> None:
    server = echo_server()
    created = 0

    def factory() -> _MCPClientBackend:
        nonlocal created
        created += 1
        return _MCPClientBackend(Client(server))

    pool = _MCPConnectionPool({"echo": factory}, timedelta(minutes=5))
    try:
        for _ in range(2):
            async with pool.backend(
                "echo",
                factory_argument=None,
            ) as client:
                result = await client.call_tool("echo", {"value": "hi"}, None)
                assert result.is_error is False
        assert created == 1
    finally:
        await pool.close()


async def test_non_none_factory_argument_always_uses_fresh_connection() -> None:
    server = echo_server()
    arguments: list[Any] = []

    def factory(argument: Any) -> _MCPClientBackend:
        arguments.append(argument)
        return _MCPClientBackend(Client(server))

    pool = _MCPConnectionPool({"echo": cast(Any, factory)}, timedelta(minutes=5))
    try:
        for _ in range(2):
            async with pool.backend(
                "echo",
                factory_argument={"tenant": "acme"},
            ):
                pass
        assert arguments == [{"tenant": "acme"}, {"tenant": "acme"}]
    finally:
        await pool.close()


async def test_legacy_fallback_is_not_reused() -> None:
    server = echo_server()
    created = 0

    def factory() -> _MCPClientBackend:
        nonlocal created
        created += 1
        return _MCPClientBackend(Client(server, mode="legacy"))

    pool = _MCPConnectionPool({"echo": factory}, timedelta(minutes=5))
    try:
        for _ in range(2):
            async with pool.backend(
                "echo",
                factory_argument=None,
            ):
                pass
        assert created == 2
    finally:
        await pool.close()


@pytest.mark.parametrize("idle_timeout", [None, timedelta(minutes=5)])
async def test_connection_reused_without_idle_eviction(
    idle_timeout: timedelta | None,
) -> None:
    server = echo_server()
    created = 0

    def factory() -> _MCPClientBackend:
        nonlocal created
        created += 1
        return _MCPClientBackend(Client(server))

    pool = _MCPConnectionPool({"echo": factory}, idle_timeout)
    try:
        for _ in range(2):
            async with pool.backend(
                "echo",
                factory_argument=None,
            ):
                pass
        assert created == 1
    finally:
        await pool.close()


async def test_zero_idle_timeout_disables_reuse() -> None:
    server = echo_server()
    created = 0

    def factory() -> _MCPClientBackend:
        nonlocal created
        created += 1
        return _MCPClientBackend(Client(server))

    pool = _MCPConnectionPool({"echo": factory}, timedelta(0))
    try:
        for _ in range(2):
            async with pool.backend(
                "echo",
                factory_argument=None,
            ):
                pass
        assert created == 2
    finally:
        await pool.close()


async def test_operation_failure_evicts_connection() -> None:
    server = echo_server()
    created = 0

    def factory() -> _MCPClientBackend:
        nonlocal created
        created += 1
        return _MCPClientBackend(Client(server))

    pool = _MCPConnectionPool({"echo": factory}, timedelta(minutes=5))
    try:
        with pytest.raises(RuntimeError, match="connection failed"):
            async with pool.backend(
                "echo",
                factory_argument=None,
            ):
                raise RuntimeError("connection failed")
        async with pool.backend(
            "echo",
            factory_argument=None,
        ):
            pass
        assert created == 2
    finally:
        await pool.close()


async def test_failure_does_not_close_a_concurrently_used_connection() -> None:
    server = echo_server()
    created = 0

    def factory() -> _MCPClientBackend:
        nonlocal created
        created += 1
        return _MCPClientBackend(Client(server))

    pool = _MCPConnectionPool({"echo": factory}, timedelta(minutes=5))
    try:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def survivor() -> Any:
            async with pool.backend("echo", factory_argument=None) as client:
                entered.set()
                await release.wait()
                return await client.call_tool("echo", {"value": "hi"}, None)

        task = asyncio.create_task(survivor())
        await entered.wait()

        # A peer operation failing must not tear the shared connection out from
        # under the operation still in flight on it.
        with pytest.raises(RuntimeError, match="connection failed"):
            async with pool.backend("echo", factory_argument=None):
                raise RuntimeError("connection failed")

        release.set()
        result = await task
        assert result.is_error is False
        assert created == 1

        # The retired connection is still not handed to later operations.
        async with pool.backend("echo", factory_argument=None):
            pass
        assert created == 2
    finally:
        await pool.close()


def test_negative_idle_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        _MCPConnectionPool({}, timedelta(seconds=-1))


async def test_pending_eviction_does_not_close_an_in_use_connection() -> None:
    server = echo_server()
    created = 0

    def factory() -> _MCPClientBackend:
        nonlocal created
        created += 1
        return _MCPClientBackend(Client(server))

    pool = _MCPConnectionPool({"echo": factory}, timedelta(minutes=5))
    try:
        async with pool.backend("echo", factory_argument=None):
            pass
        key = next(iter(pool._records))
        record = pool._records[key]

        entered = asyncio.Event()
        release = asyncio.Event()

        async def survivor() -> Any:
            async with pool.backend("echo", factory_argument=None) as client:
                entered.set()
                await release.wait()
                return await client.call_tool("echo", {"value": "hi"}, None)

        task = asyncio.create_task(survivor())
        await entered.wait()

        # A peer failure retires and unmaps the record while the survivor is
        # still in flight on it.
        with pytest.raises(RuntimeError, match="connection failed"):
            async with pool.backend("echo", factory_argument=None):
                raise RuntimeError("connection failed")

        # An idle-eviction task armed before either operation acquired the
        # record now runs. It cannot be cancelled once created, so it must
        # notice the record is still in use rather than closing the transport.
        await pool._evict(key, record, only_if_idle=True)

        release.set()
        result = await task
        assert result.is_error is False
        assert created == 1
    finally:
        await pool.close()


async def test_idle_timeout_evicts_the_connection() -> None:
    server = echo_server()
    created = 0

    def factory() -> _MCPClientBackend:
        nonlocal created
        created += 1
        return _MCPClientBackend(Client(server))

    pool = _MCPConnectionPool({"echo": factory}, timedelta(milliseconds=10))
    try:
        async with pool.backend("echo", factory_argument=None):
            pass
        assert created == 1

        # The eviction runs in a task the pool must keep a strong reference to;
        # an unretained one can be collected before it closes the connection.
        for _ in range(100):
            if not pool._records:
                break
            await asyncio.sleep(0.01)
        assert not pool._records

        async with pool.backend("echo", factory_argument=None):
            pass
        assert created == 2
    finally:
        await pool.close()


async def test_close_leaves_no_state_for_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = echo_server()

    def factory() -> _MCPClientBackend:
        return _MCPClientBackend(Client(server))

    pool = _MCPConnectionPool({"echo": factory}, timedelta(milliseconds=50))
    async with pool.backend("echo", factory_argument=None):
        pass
    assert pool._locks

    record = next(iter(pool._records.values()))
    original_close = record.close
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    async def delayed_close() -> None:
        close_started.set()
        await allow_close.wait()
        await original_close()

    # Keep close in progress until the idle timer would have fired. This makes
    # the teardown race deterministic on event loops where it is otherwise rare.
    monkeypatch.setattr(record, "close", delayed_close)
    close_task = asyncio.create_task(pool.close())
    await close_started.wait()
    await asyncio.sleep(0.1)
    allow_close.set()
    await close_task

    # Locks are keyed by event loop alongside the records, so leaving them
    # behind keeps every loop the pool has served alive.
    assert not pool._records
    assert not pool._locks
    assert not pool._evictions
