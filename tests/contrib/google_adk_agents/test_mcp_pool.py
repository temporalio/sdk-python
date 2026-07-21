# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the pooled/idle-evicted MCP connection support in
``temporalio.contrib.google_adk_agents._mcp``.

These exercise ``TemporalMcpToolSetProvider``'s generated activities directly
against a fake ``McpToolset``, so they don't require a real MCP server, a
Temporal test server, or a full ``Worker`` -- they're targeted regression
tests for the connection-leak fix (one underlying toolset per name, reused
across calls, evicted on idle or on error).
"""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from google.adk.events import EventActions

from temporalio.contrib.google_adk_agents import _mcp
from temporalio.contrib.google_adk_agents._mcp import (
    TemporalMcpToolSetProvider,
    TemporalToolContext,
    _CallToolArguments,
    _GetToolsArguments,
)


class _FakeTool:
    def __init__(self, name: str, *, fail_run: bool = False) -> None:
        self.name = name
        self.description = "a fake tool"
        self.is_long_running = False
        self.custom_metadata: dict[str, Any] | None = None
        self._fail_run = fail_run

    def _get_declaration(self) -> None:
        return None

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        if self._fail_run:
            raise RuntimeError("tool call failed")
        return {"echo": args}


class _FakeToolset:
    """Stands in for ``google.adk.tools.mcp_tool.McpToolset``.

    Tracks whether ``close()`` was called so tests can assert eviction
    actually tears the pooled connection down, not just drops it from the
    cache.
    """

    def __init__(self, *, fail_get_tools: bool = False, fail_run: bool = False) -> None:
        self.closed = False
        self._fail_get_tools = fail_get_tools
        self._fail_run = fail_run

    async def get_tools(self) -> list[_FakeTool]:
        if self._fail_get_tools:
            raise RuntimeError("get_tools failed")
        return [_FakeTool("echo", fail_run=self._fail_run)]

    async def close(self) -> None:
        self.closed = True


def _tool_context() -> TemporalToolContext:
    return TemporalToolContext(
        tool_confirmation=None,
        function_call_id=None,
        event_actions=EventActions(),
    )


def _call_tool_args(factory_argument: Any = None) -> _CallToolArguments:
    return _CallToolArguments(
        factory_argument=factory_argument,
        name="echo",
        arguments={"x": 1},
        tool_context=_tool_context(),
    )


@pytest.fixture(autouse=True)
def _clear_connection_pool():
    _mcp._CONNECTIONS.clear()
    yield
    _mcp._CONNECTIONS.clear()


async def test_call_tool_reuses_one_pooled_connection():
    """N sequential call_tool executions reuse one toolset, not N."""
    created: list[_FakeToolset] = []

    def factory(_: Any) -> _FakeToolset:
        toolset = _FakeToolset()
        created.append(toolset)
        return toolset

    provider = TemporalMcpToolSetProvider("pool_reuse", factory)
    _, call_tool = provider._get_activities()

    for _ in range(5):
        result = await call_tool(_call_tool_args())
        assert result.result == {"echo": {"x": 1}}

    assert len(created) == 1
    assert not created[0].closed


async def test_get_tools_and_call_tool_share_one_connection():
    """list-tools and call-tool activities for the same name share a connection."""
    created: list[_FakeToolset] = []

    def factory(_: Any) -> _FakeToolset:
        toolset = _FakeToolset()
        created.append(toolset)
        return toolset

    provider = TemporalMcpToolSetProvider("pool_shared", factory)
    get_tools, call_tool = provider._get_activities()

    tools = await get_tools(_GetToolsArguments(factory_argument=None))
    assert [t.name for t in tools] == ["echo"]

    await call_tool(_call_tool_args())
    await call_tool(_call_tool_args())

    assert len(created) == 1


async def test_idle_eviction_closes_connection_after_timeout():
    def factory(_: Any) -> _FakeToolset:
        return _FakeToolset()

    provider = TemporalMcpToolSetProvider(
        "pool_idle",
        factory,
        mcp_connection_idle_timeout=timedelta(milliseconds=20),
    )
    _, call_tool = provider._get_activities()

    await call_tool(_call_tool_args())

    record = _mcp._CONNECTIONS["pool_idle"]
    assert not record.toolset.closed

    for _ in range(100):
        if "pool_idle" not in _mcp._CONNECTIONS:
            break
        await asyncio.sleep(0.01)

    assert "pool_idle" not in _mcp._CONNECTIONS
    assert record.toolset.closed


async def test_idle_timer_only_arms_once_all_inflight_calls_release():
    """Two overlapping callers must not let the idle timer fire mid-call.

    Exercises ``_ConnectionRecord`` directly (rather than through the
    activities) so the ordering of acquire/release calls -- which stands in
    for two concurrent in-flight activity invocations sharing one pooled
    connection -- is deterministic instead of depending on how the event loop
    happens to interleave coroutines that never truly suspend.
    """
    toolset = _FakeToolset()
    record = _mcp._ConnectionRecord("pool_inflight", toolset, timedelta(milliseconds=1))
    _mcp._CONNECTIONS["pool_inflight"] = record

    record.acquire()
    record.acquire()
    assert record._idle_handle is None

    record.release()
    # One caller is still in flight; the idle timer must stay disarmed.
    assert record._idle_handle is None
    assert "pool_inflight" in _mcp._CONNECTIONS

    record.release()
    # Now that both callers are done, the idle timer arms.
    assert record._idle_handle is not None

    for _ in range(100):
        if "pool_inflight" not in _mcp._CONNECTIONS:
            break
        await asyncio.sleep(0.01)
    assert "pool_inflight" not in _mcp._CONNECTIONS
    assert toolset.closed


async def test_call_tool_error_evicts_and_next_call_reconnects():
    created: list[_FakeToolset] = []

    def factory(_: Any) -> _FakeToolset:
        # Only the first toolset's get_tools() fails.
        toolset = _FakeToolset(fail_get_tools=(len(created) == 0))
        created.append(toolset)
        return toolset

    provider = TemporalMcpToolSetProvider("pool_error", factory)
    _, call_tool = provider._get_activities()

    with pytest.raises(RuntimeError, match="get_tools failed"):
        await call_tool(_call_tool_args())

    assert "pool_error" not in _mcp._CONNECTIONS
    assert created[0].closed

    result = await call_tool(_call_tool_args())
    assert result.result == {"echo": {"x": 1}}
    assert len(created) == 2
    assert not created[1].closed


async def test_call_tool_run_async_error_evicts_broken_connection():
    created: list[_FakeToolset] = []

    def factory(_: Any) -> _FakeToolset:
        toolset = _FakeToolset(fail_run=(len(created) == 0))
        created.append(toolset)
        return toolset

    provider = TemporalMcpToolSetProvider("pool_run_error", factory)
    _, call_tool = provider._get_activities()

    with pytest.raises(RuntimeError, match="tool call failed"):
        await call_tool(_call_tool_args())

    assert "pool_run_error" not in _mcp._CONNECTIONS
    assert created[0].closed

    result = await call_tool(_call_tool_args())
    assert result.result == {"echo": {"x": 1}}
    assert len(created) == 2


async def test_call_tool_no_matching_tool_does_not_evict():
    """A business-logic ApplicationError isn't a broken session; keep pooling."""
    created: list[_FakeToolset] = []

    def factory(_: Any) -> _FakeToolset:
        toolset = _FakeToolset()
        created.append(toolset)
        return toolset

    provider = TemporalMcpToolSetProvider("pool_no_match", factory)
    _, call_tool = provider._get_activities()

    args = _CallToolArguments(
        factory_argument=None,
        name="does_not_exist",
        arguments={},
        tool_context=_tool_context(),
    )
    with pytest.raises(Exception, match="Unable to find matching mcp tool"):
        await call_tool(args)

    assert "pool_no_match" in _mcp._CONNECTIONS
    assert not created[0].closed

    # A subsequent successful call reuses the same toolset instance.
    await call_tool(_call_tool_args())
    assert len(created) == 1
