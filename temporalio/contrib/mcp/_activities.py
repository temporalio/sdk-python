# pyright: reportUnusedClass=false

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, TypeVar

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


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


class _MCPActivities:
    """Build framework-neutral MCP operation Activities for named backends."""

    def __init__(
        self,
        factories: dict[str, _MCPBackendFactory],
        idle_timeout: timedelta | None = timedelta(minutes=5),
    ) -> None:
        if len(factories) != len(set(factories)):
            raise ValueError("MCP client names must be unique")
        self._factories = dict(factories)
        self._pool = _MCPConnectionPool(self._factories, idle_timeout)
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
            ) -> list[dict[str, Any]]:
                parsed = _MCPRequest(**request)
                return [
                    _dump(value)
                    for value in await self._run(
                        server,
                        parsed,
                        lambda backend: backend.list_tools(parsed.meta),
                    )
                ]

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
            ) -> list[dict[str, Any]]:
                parsed = _MCPRequest(**request)
                return [
                    _dump(value)
                    for value in await self._run(
                        server,
                        parsed,
                        lambda backend: backend.list_prompts(parsed.meta),
                    )
                ]

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
                        parsed.meta,
                    ),
                )
                return _dump(result)

            @activity.defn(name=_activity_name(server, "list-resources"))
            async def list_resources(
                request: dict[str, Any], server: str = server
            ) -> list[dict[str, Any]]:
                parsed = _MCPRequest(**request)
                return [
                    _dump(value)
                    for value in await self._run(
                        server,
                        parsed,
                        lambda backend: backend.list_resources(parsed.meta),
                    )
                ]

            @activity.defn(name=_activity_name(server, "list-resource-templates"))
            async def list_resource_templates(
                request: dict[str, Any], server: str = server
            ) -> list[dict[str, Any]]:
                parsed = _MCPRequest(**request)
                return [
                    _dump(value)
                    for value in await self._run(
                        server,
                        parsed,
                        lambda backend: backend.list_resource_templates(parsed.meta),
                    )
                ]

            @activity.defn(name=_activity_name(server, "read-resource"))
            async def read_resource(
                request: dict[str, Any], server: str = server
            ) -> dict[str, Any]:
                parsed = _ReadResourceRequest(**request)
                result = await self._run(
                    server,
                    parsed,
                    lambda backend: backend.read_resource(
                        parsed.uri,
                        parsed.meta,
                    ),
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
    async def run_context(self):
        try:
            yield
        finally:
            await self._pool.close()
