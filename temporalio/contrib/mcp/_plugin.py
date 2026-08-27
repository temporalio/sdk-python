"""Worker plugin for durable MCP clients."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any, TypeAlias

from mcp import Client

from temporalio.contrib.mcp._activities import _MCPActivities
from temporalio.contrib.mcp._client import _mcp_client_backend_factory
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

MCPClientFactory: TypeAlias = Callable[[], Client] | Callable[[Any], Client]
"""Factory for an MCP client, optionally parameterized by workflow input."""


class MCPPlugin(SimplePlugin):
    """Register worker-side MCP clients for use from Temporal workflows.

    This class is experimental and may change in future versions.

    The mapping keys are durable server names used by
    :class:`temporalio.contrib.mcp.TemporalMCPClient`. Factories and transports
    remain on the worker, so credentials are not captured in workflow history.

    A factory may declare one positional parameter. It receives the workflow
    client's ``factory_argument`` (``None`` when no argument was supplied).
    Parameterless modern connections are reused until they have been idle for
    ``connection_idle_timeout``. A non-None factory argument selects a fresh
    connection for each Activity.
    """

    def __init__(
        self,
        clients: Mapping[str, MCPClientFactory],
        *,
        connection_idle_timeout: timedelta | None = timedelta(minutes=5),
    ) -> None:
        """Create an MCP plugin for the named client factories.

        Args:
            clients: Mapping of durable names to worker-side MCP client
                factories.
            connection_idle_timeout: How long an idle modern MCP connection is
                reused. ``None`` disables idle eviction; zero disables reuse.
        """
        mcp_activities = _MCPActivities(
            {
                name: _mcp_client_backend_factory(name, factory)
                for name, factory in clients.items()
            },
            idle_timeout=connection_idle_timeout,
        )
        self._mcp_activities = mcp_activities

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if runner is None:
                raise ValueError("No WorkflowRunner provided to the MCP plugin")
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules("mcp"),
                )
            return runner

        super().__init__(
            name="MCPPlugin",
            activities=mcp_activities.activities,
            workflow_runner=workflow_runner,
            run_context=mcp_activities.run_context,
        )
