# pyright: reportUnusedClass=false

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, TypeVar

from mcp.types import (
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
)
from pydantic import BaseModel

from temporalio import activity
from temporalio.contrib.mcp._activity import (
    _activity_name,
    _CallToolRequest,
    _GetPromptRequest,
    _MCPRequest,
    _ReadResourceRequest,
)
from temporalio.contrib.mcp._backend import _MCPBackend, _MCPBackendFactory
from temporalio.contrib.mcp._pool import _MCPConnectionPool

_Result = TypeVar("_Result")

logger = logging.getLogger(__name__)

# Upper bound on how long worker shutdown waits for MCP transports to close.
_CLOSE_TIMEOUT_SECONDS = 10.0


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


class _MCPActivities:
    """Build framework-neutral MCP operation Activities for named backends."""

    def __init__(
        self,
        factories: dict[str, _MCPBackendFactory],
        idle_timeout: timedelta | None = timedelta(minutes=5),
    ) -> None:
        self._factories = dict(factories)
        self._pool = _MCPConnectionPool(self._factories, idle_timeout)
        self._abandoned_closes: set[asyncio.Task[None]] = set()
        self.activities = self._build_activities()

    async def _run(
        self,
        server: str,
        request: _MCPRequest,
        operation: Callable[[_MCPBackend], Awaitable[_Result]],
    ) -> _Result:
        async with self._pool.backend(
            server,
            factory_argument=request.factory_argument,
        ) as backend:
            return await operation(backend)

    def _build_activities(self) -> Sequence[Callable[..., Any]]:
        activities: list[Callable[..., Any]] = []
        for server in self._factories:

            @activity.defn(name=_activity_name(server, "list-tools"))
            async def list_tools(
                request: dict[str, Any], server: str = server
            ) -> dict[str, Any]:
                parsed = _MCPRequest(**request)
                return _dump(
                    ListToolsResult(
                        tools=await self._run(
                            server,
                            parsed,
                            lambda backend: backend.list_tools(),
                        )
                    )
                )

            @activity.defn(name=_activity_name(server, "call-tool"))
            async def call_tool(
                request: dict[str, Any], server: str = server
            ) -> dict[str, Any]:
                parsed = _CallToolRequest(**request)
                result = await self._run(
                    server,
                    parsed,
                    lambda backend: backend.call_tool(
                        parsed.name,
                        parsed.arguments,
                        parsed.meta,
                    ),
                )
                return _dump(result)

            @activity.defn(name=_activity_name(server, "list-prompts"))
            async def list_prompts(
                request: dict[str, Any], server: str = server
            ) -> dict[str, Any]:
                parsed = _MCPRequest(**request)
                return _dump(
                    ListPromptsResult(
                        prompts=await self._run(
                            server,
                            parsed,
                            lambda backend: backend.list_prompts(),
                        )
                    )
                )

            @activity.defn(name=_activity_name(server, "get-prompt"))
            async def get_prompt(
                request: dict[str, Any], server: str = server
            ) -> dict[str, Any]:
                parsed = _GetPromptRequest(**request)
                result = await self._run(
                    server,
                    parsed,
                    lambda backend: backend.get_prompt(
                        parsed.name,
                        parsed.arguments,
                    ),
                )
                return _dump(result)

            @activity.defn(name=_activity_name(server, "list-resources"))
            async def list_resources(
                request: dict[str, Any], server: str = server
            ) -> dict[str, Any]:
                parsed = _MCPRequest(**request)
                return _dump(
                    ListResourcesResult(
                        resources=await self._run(
                            server,
                            parsed,
                            lambda backend: backend.list_resources(),
                        )
                    )
                )

            @activity.defn(name=_activity_name(server, "list-resource-templates"))
            async def list_resource_templates(
                request: dict[str, Any], server: str = server
            ) -> dict[str, Any]:
                parsed = _MCPRequest(**request)
                return _dump(
                    ListResourceTemplatesResult(
                        resource_templates=await self._run(
                            server,
                            parsed,
                            lambda backend: backend.list_resource_templates(),
                        )
                    )
                )

            @activity.defn(name=_activity_name(server, "read-resource"))
            async def read_resource(
                request: dict[str, Any], server: str = server
            ) -> dict[str, Any]:
                parsed = _ReadResourceRequest(**request)
                result = await self._run(
                    server,
                    parsed,
                    lambda backend: backend.read_resource(parsed.uri),
                )
                return _dump(result)

            activities.extend(
                (
                    list_tools,
                    call_tool,
                    list_prompts,
                    get_prompt,
                    list_resources,
                    list_resource_templates,
                    read_resource,
                )
            )
        return activities

    @asynccontextmanager
    async def run_context(self) -> AsyncIterator[None]:
        body_completed = False
        try:
            yield
            body_completed = True
        finally:
            close_task = asyncio.create_task(self._pool.close())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                # Worker context exit cancels its wrapper after the inner run
                # completes. Finish MCP cleanup without swallowing cancellation
                # received while the worker was still running.
                if not body_completed:
                    raise
                await self._finish_close(close_task)

    async def _finish_close(self, close_task: asyncio.Task[None]) -> None:
        """Wait a bounded time for a close whose cancellation was swallowed."""
        try:
            await asyncio.wait_for(asyncio.shield(close_task), _CLOSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # An unresponsive MCP server must not hang worker shutdown
            # indefinitely, especially now that there is no cancellation left to
            # break out with. Leave the close running, but strongly referenced
            # so it is not garbage collected part way through.
            self._abandoned_closes.add(close_task)
            close_task.add_done_callback(self._abandoned_closes.discard)
            logger.warning(
                "Timed out after %s seconds closing MCP connections; "
                "an MCP server may not have shut down cleanly.",
                _CLOSE_TIMEOUT_SECONDS,
            )
