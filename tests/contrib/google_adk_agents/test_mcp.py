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

"""Unit tests for the stateless MCP toolset support in
``temporalio.contrib.google_adk_agents._mcp``.

These exercise ``TemporalMcpToolSetProvider``'s generated activities directly
against a fake ``McpToolset``, so they don't require a real MCP server, a
Temporal test server, or a full ``Worker``. They are targeted regression tests
for the connection-leak fix (issue #1663): every call builds its own toolset,
honors that call's ``factory_argument``, and always closes the toolset.
"""

from typing import Any, cast

import pytest
from google.adk.events import EventActions
from google.adk.tools.mcp_tool import McpToolset

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

    Tracks whether ``close()`` was called so tests can assert the stateless
    path always tears the toolset down, and records the ``factory_argument``
    it was created with so tests can prove each call routes with its own
    argument.
    """

    def __init__(
        self,
        factory_argument: Any = None,
        *,
        fail_get_tools: bool = False,
        fail_run: bool = False,
    ) -> None:
        self.factory_argument = factory_argument
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


async def test_call_tool_creates_and_closes_fresh_toolset_each_call():
    """Each call_tool builds its own toolset and closes it, every time."""
    created: list[_FakeToolset] = []

    def factory(arg: Any) -> McpToolset:
        toolset = _FakeToolset(arg)
        created.append(toolset)
        return cast(McpToolset, cast(object, toolset))

    provider = TemporalMcpToolSetProvider("stateless_reuse", factory)
    _, call_tool = provider._get_activities()

    for _ in range(5):
        result = await call_tool(_call_tool_args())
        assert result.result == {"echo": {"x": 1}}

    # One fresh toolset per call, and every one was closed.
    assert len(created) == 5
    assert all(t.closed for t in created)


async def test_get_tools_creates_and_closes_fresh_toolset():
    created: list[_FakeToolset] = []

    def factory(arg: Any) -> McpToolset:
        toolset = _FakeToolset(arg)
        created.append(toolset)
        return cast(McpToolset, cast(object, toolset))

    provider = TemporalMcpToolSetProvider("stateless_list", factory)
    get_tools, _ = provider._get_activities()

    tools = await get_tools(_GetToolsArguments(factory_argument=None))
    assert [t.name for t in tools] == ["echo"]

    assert len(created) == 1
    assert created[0].closed


async def test_factory_argument_honored_on_every_call():
    """The bug the reviewer flagged: a later call with a different
    ``factory_argument`` must route with *its own* argument, never silently
    reuse a connection opened for an earlier argument.
    """
    created: list[_FakeToolset] = []

    def factory(arg: Any) -> McpToolset:
        toolset = _FakeToolset(arg)
        created.append(toolset)
        return cast(McpToolset, cast(object, toolset))

    provider = TemporalMcpToolSetProvider("stateless_routing", factory)
    _, call_tool = provider._get_activities()

    await call_tool(_call_tool_args(factory_argument="tenant-a"))
    await call_tool(_call_tool_args(factory_argument="tenant-b"))

    assert [t.factory_argument for t in created] == ["tenant-a", "tenant-b"]
    assert all(t.closed for t in created)


async def test_call_tool_closes_toolset_on_error():
    """A failure mid-call still closes the toolset (no leak on the error path)."""
    created: list[_FakeToolset] = []

    def factory(arg: Any) -> McpToolset:
        toolset = _FakeToolset(arg, fail_run=True)
        created.append(toolset)
        return cast(McpToolset, cast(object, toolset))

    provider = TemporalMcpToolSetProvider("stateless_run_error", factory)
    _, call_tool = provider._get_activities()

    with pytest.raises(RuntimeError, match="tool call failed"):
        await call_tool(_call_tool_args())

    assert len(created) == 1
    assert created[0].closed


async def test_get_tools_closes_toolset_on_error():
    created: list[_FakeToolset] = []

    def factory(arg: Any) -> McpToolset:
        toolset = _FakeToolset(arg, fail_get_tools=True)
        created.append(toolset)
        return cast(McpToolset, cast(object, toolset))

    provider = TemporalMcpToolSetProvider("stateless_list_error", factory)
    get_tools, _ = provider._get_activities()

    with pytest.raises(RuntimeError, match="get_tools failed"):
        await get_tools(_GetToolsArguments(factory_argument=None))

    assert len(created) == 1
    assert created[0].closed


async def test_call_tool_no_matching_tool_still_closes():
    """A business-logic ApplicationError still closes the fresh toolset."""
    created: list[_FakeToolset] = []

    def factory(arg: Any) -> McpToolset:
        toolset = _FakeToolset(arg)
        created.append(toolset)
        return cast(McpToolset, cast(object, toolset))

    provider = TemporalMcpToolSetProvider("stateless_no_match", factory)
    _, call_tool = provider._get_activities()

    args = _CallToolArguments(
        factory_argument=None,
        name="does_not_exist",
        arguments={},
        tool_context=_tool_context(),
    )
    with pytest.raises(Exception, match="Unable to find matching mcp tool"):
        await call_tool(args)

    assert len(created) == 1
    assert created[0].closed
