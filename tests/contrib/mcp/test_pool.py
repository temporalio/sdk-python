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


def test_negative_idle_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        _MCPConnectionPool({}, timedelta(seconds=-1))
