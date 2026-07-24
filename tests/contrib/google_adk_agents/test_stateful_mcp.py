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

"""Integration tests for the stateful (pooled) MCP toolset support in
``temporalio.contrib.google_adk_agents._mcp``.

These drive the real ``connect`` -> dedicated-worker -> ``get_tools`` path
through a Temporal workflow, but against an in-memory fake ``McpToolset`` (no
subprocess, no real MCP server) so they run in CI. They prove the properties
the reviewer asked for: one toolset is built per workflow run, ``factory_argument``
is consumed exactly once per run, distinct runs never share a toolset, and the
toolset is torn down when the workflow completes.
"""

import uuid
from datetime import timedelta
from typing import Any, cast

from google.adk.tools.mcp_tool import McpToolset

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from temporalio.contrib.google_adk_agents import (
        GoogleAdkPlugin,
        TemporalStatefulMcpToolSet,
        TemporalStatefulMcpToolSetProvider,
    )

# Populated inside the activity worker process (same process as the test) each
# time the toolset factory runs, so tests can inspect what was built.
CREATED: list["_FakeToolset"] = []


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "a fake tool"
        self.is_long_running = False
        self.custom_metadata: dict[str, Any] | None = None

    def _get_declaration(self) -> None:
        return None

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        return {"echo": args}


class _FakeToolset:
    """In-memory stand-in for ``google.adk.tools.mcp_tool.McpToolset``."""

    def __init__(self, factory_argument: Any = None) -> None:
        self.factory_argument = factory_argument
        self.get_tools_calls = 0
        self.closed = False

    async def get_tools(self) -> list[_FakeTool]:
        self.get_tools_calls += 1
        return [_FakeTool("echo")]

    async def close(self) -> None:
        self.closed = True


def _factory(arg: Any) -> McpToolset:
    toolset = _FakeToolset(arg)
    CREATED.append(toolset)
    return cast(McpToolset, cast(object, toolset))


@workflow.defn
class StatefulMcpWorkflow:
    @workflow.run
    async def run(self, factory_argument: Any | None) -> list[str]:
        async with TemporalStatefulMcpToolSet(
            "stateful_set",
            factory_argument=factory_argument,
        ) as toolset:
            # Two calls against the same persistent toolset -- exercises reuse.
            tools_1 = await toolset.get_tools()
            tools_2 = await toolset.get_tools()
            return [t.name for t in tools_1] + [t.name for t in tools_2]


def _make_client(
    client: Client, provider: TemporalStatefulMcpToolSetProvider
) -> Client:
    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin(toolset_providers=[provider])]
    return Client(**new_config)


async def test_stateful_one_toolset_per_run_and_teardown(client: Client):
    CREATED.clear()
    provider = TemporalStatefulMcpToolSetProvider("stateful_set", _factory)
    client = _make_client(client, provider)

    async with Worker(
        client,
        task_queue="adk-stateful-mcp",
        workflows=[StatefulMcpWorkflow],
        max_cached_workflows=0,
    ):
        result = await client.execute_workflow(
            StatefulMcpWorkflow.run,
            None,
            id=f"stateful-mcp-{uuid.uuid4()}",
            task_queue="adk-stateful-mcp",
            execution_timeout=timedelta(seconds=60),
        )

    assert result == ["echo", "echo"]
    # Exactly one toolset built for the whole run...
    assert len(CREATED) == 1
    # ...reused across both get_tools calls (state maintained, not per-call)...
    assert CREATED[0].get_tools_calls == 2
    # ...and closed when the workflow completed (no leak).
    assert CREATED[0].closed
    # The dedicated-worker toolset registry is empty after teardown.
    assert provider._toolsets == {}


async def test_stateful_factory_argument_consumed_once(client: Client):
    CREATED.clear()
    provider = TemporalStatefulMcpToolSetProvider("stateful_set", _factory)
    client = _make_client(client, provider)

    async with Worker(
        client,
        task_queue="adk-stateful-mcp-arg",
        workflows=[StatefulMcpWorkflow],
        max_cached_workflows=0,
    ):
        await client.execute_workflow(
            StatefulMcpWorkflow.run,
            {"tenant": "acme"},
            id=f"stateful-mcp-{uuid.uuid4()}",
            task_queue="adk-stateful-mcp-arg",
            execution_timeout=timedelta(seconds=60),
        )

    assert len(CREATED) == 1
    assert CREATED[0].factory_argument == {"tenant": "acme"}


async def test_stateful_no_cross_run_sharing(client: Client):
    CREATED.clear()
    provider = TemporalStatefulMcpToolSetProvider("stateful_set", _factory)
    client = _make_client(client, provider)

    async with Worker(
        client,
        task_queue="adk-stateful-mcp-iso",
        workflows=[StatefulMcpWorkflow],
        max_cached_workflows=0,
    ):
        await client.execute_workflow(
            StatefulMcpWorkflow.run,
            "tenant-a",
            id=f"stateful-mcp-a-{uuid.uuid4()}",
            task_queue="adk-stateful-mcp-iso",
            execution_timeout=timedelta(seconds=60),
        )
        await client.execute_workflow(
            StatefulMcpWorkflow.run,
            "tenant-b",
            id=f"stateful-mcp-b-{uuid.uuid4()}",
            task_queue="adk-stateful-mcp-iso",
            execution_timeout=timedelta(seconds=60),
        )

    # Two distinct runs -> two distinct toolsets, each with its own argument.
    assert len(CREATED) == 2
    assert {t.factory_argument for t in CREATED} == {"tenant-a", "tenant-b"}
    assert all(t.closed for t in CREATED)
    assert provider._toolsets == {}
