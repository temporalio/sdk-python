import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import EventActions
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from google.genai.types import FunctionDeclaration

from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig

# Default time an idle pooled McpToolset connection stays open before being
# closed. The timer resets on every call that reuses the connection.
# Overridable via ``TemporalMcpToolSetProvider(mcp_connection_idle_timeout=...)``,
# matching the ``_MCP_CONNECTION_IDLE`` default used by the strands and
# google_genai contribs' equivalent MCP connection pools.
_MCP_CONNECTION_IDLE = timedelta(minutes=5)

# Provider name -> live pooled connection held open in the activity worker
# process. Activities run in the worker process, so this module state is
# shared across activity invocations on the worker.
_CONNECTIONS: dict[str, "_ConnectionRecord"] = {}


class _ConnectionRecord:
    """A single ``McpToolset`` instance held open and reused across calls.

    ``McpToolset`` lazily opens its MCP session on first use and manages
    reconnection internally via its own ``MCPSessionManager``, so -- unlike
    the raw ``mcp.ClientSession`` pooled by the strands/google_genai contribs
    -- no dedicated owner task is needed here: the toolset instance itself is
    the thing kept alive and reused across activity invocations.
    """

    def __init__(self, name: str, toolset: McpToolset, idle_timeout: timedelta) -> None:
        self._name = name
        self._toolset = toolset
        self._idle_timeout = idle_timeout
        self._idle_handle: asyncio.TimerHandle | None = None
        self._inflight = 0

    @property
    def toolset(self) -> McpToolset:
        return self._toolset

    def acquire(self) -> None:
        """Mark a call in flight; pause idle eviction while calls are active."""
        self._inflight += 1
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def release(self) -> None:
        """Mark a call done; arm idle eviction once no calls remain in flight."""
        self._inflight -= 1
        # Only the record still cached under this name arms a timer; a record
        # already evicted or never cached must not schedule one, or it could
        # later evict a different, healthy connection for the same name.
        if self._inflight == 0 and _CONNECTIONS.get(self._name) is self:
            loop = asyncio.get_running_loop()
            self._idle_handle = loop.call_later(
                self._idle_timeout.total_seconds(), self._on_idle
            )

    def _on_idle(self) -> None:
        asyncio.ensure_future(self._maybe_evict())

    async def _maybe_evict(self) -> None:
        # A call may have acquired the connection between the timer firing and
        # this task running; only evict if it is still idle.
        if self._inflight == 0:
            await _evict_connection(self._name)

    async def aclose(self) -> None:
        """Cancel any pending idle timer and close the underlying toolset."""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        await self._toolset.close()


async def get_connection(
    name: str,
    toolset_factory: Callable[[Any | None], McpToolset],
    factory_argument: Any | None,
    idle_timeout: timedelta,
) -> "_ConnectionRecord":
    """Return the cached connection for ``name``, opening one lazily if needed.

    The returned record is acquired; the caller must ``release()`` it once the
    call completes so idle eviction can resume. ``factory_argument`` is only
    consulted the first time a connection is opened for ``name`` -- a warm
    connection is reused as-is regardless of subsequent calls' ``factory_argument``
    values, matching how the strands/google_genai contribs pool a single
    connection per name.
    """
    record = _CONNECTIONS.get(name)
    if record is None:
        record = _ConnectionRecord(
            name, toolset_factory(factory_argument), idle_timeout
        )
        _CONNECTIONS[name] = record
    record.acquire()
    return record


async def _evict_connection(name: str) -> None:
    record = _CONNECTIONS.pop(name, None)
    if record is not None:
        await record.aclose()


@dataclass
class _GetToolsArguments:
    factory_argument: Any | None


@dataclass
class _ToolResult:
    name: str
    description: str
    is_long_running: bool
    custom_metadata: dict[str, Any] | None
    function_declaration: FunctionDeclaration | None


@dataclass
class TemporalToolContext:
    """Context for tools running within Temporal workflows.

    Provides access to tool confirmation and event actions for ADK integration.
    """

    tool_confirmation: ToolConfirmation | None
    function_call_id: str | None
    event_actions: EventActions

    def request_confirmation(
        self,
        *,
        hint: str | None = None,
        payload: Any | None = None,
    ) -> None:
        """Requests confirmation for the given function call.

        Args:
          hint: A hint to the user on how to confirm the tool call.
          payload: The payload used to confirm the tool call.
        """
        if not self.function_call_id:
            raise ValueError("function_call_id is not set.")
        self.event_actions.requested_tool_confirmations[self.function_call_id] = (
            ToolConfirmation(
                hint=hint or "",
                payload=payload,
            )
        )


@dataclass
class _CallToolResult:
    result: Any
    tool_context: TemporalToolContext


@dataclass
class _CallToolArguments:
    factory_argument: Any | None
    name: str
    arguments: dict[str, Any]
    tool_context: TemporalToolContext


class TemporalMcpToolSetProvider:
    """Provider for creating Temporal-aware MCP toolsets.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Manages the creation of toolset activities and handles tool execution
    within Temporal workflows.
    """

    def __init__(
        self,
        name: str,
        toolset_factory: Callable[[Any | None], McpToolset],
        mcp_connection_idle_timeout: timedelta | None = None,
    ) -> None:
        """Initializes the toolset provider.

        Args:
            name: Name prefix for the generated activities.
            toolset_factory: Factory function that creates McpToolset instances.
            mcp_connection_idle_timeout: How long a pooled MCP connection may sit
                idle (no in-flight calls) before it is closed and evicted.
                Defaults to 5 minutes, matching the strands/google_genai contribs.
        """
        super().__init__()
        self._name = name
        self._toolset_factory = toolset_factory
        self._idle_timeout = mcp_connection_idle_timeout or _MCP_CONNECTION_IDLE

    def _get_activities(self) -> Sequence[Callable]:
        @activity.defn(name=self._name + "-list-tools")
        async def get_tools(
            args: _GetToolsArguments,
        ) -> list[_ToolResult]:
            record = await get_connection(
                self._name,
                self._toolset_factory,
                args.factory_argument,
                self._idle_timeout,
            )
            try:
                try:
                    tools = await record.toolset.get_tools()
                except Exception:
                    # The underlying session may be broken; drop it so the
                    # next call reconnects instead of reusing a dead session.
                    await _evict_connection(self._name)
                    raise
            finally:
                record.release()
            return [
                _ToolResult(
                    tool.name,
                    tool.description,
                    tool.is_long_running,
                    tool.custom_metadata,
                    tool._get_declaration(),
                )
                for tool in tools
            ]

        @activity.defn(name=self._name + "-call-tool")
        async def call_tool(
            args: _CallToolArguments,
        ) -> _CallToolResult:
            record = await get_connection(
                self._name,
                self._toolset_factory,
                args.factory_argument,
                self._idle_timeout,
            )
            try:
                try:
                    tools = await record.toolset.get_tools()
                except Exception:
                    await _evict_connection(self._name)
                    raise

                tool_match = [tool for tool in tools if tool.name == args.name]
                if len(tool_match) == 0:
                    raise ApplicationError(
                        f"Unable to find matching mcp tool by name: {args.name}"
                    )
                if len(tool_match) > 1:
                    raise ApplicationError(
                        f"Unable too many matching mcp tools by name: {args.name}"
                    )
                tool = tool_match[0]

                try:
                    # We cannot provide a full-fledged ToolContext so we need to provide only what is needed by the tool
                    result = await tool.run_async(
                        args=args.arguments,
                        tool_context=args.tool_context,  #  type:ignore
                    )
                except Exception:
                    await _evict_connection(self._name)
                    raise
            finally:
                record.release()
            return _CallToolResult(result=result, tool_context=args.tool_context)

        return get_tools, call_tool


class _TemporalTool(BaseTool):
    def __init__(
        self,
        set_name: str,
        factory_argument: Any | None,
        config: ActivityConfig | None,
        declaration: FunctionDeclaration | None,
        *,
        name: str,
        description: str,
        is_long_running: bool = False,
        custom_metadata: dict[str, Any] | None = None,
    ):
        super().__init__(
            name=name,
            description=description,
            is_long_running=is_long_running,
            custom_metadata=custom_metadata,
        )
        self._set_name = set_name
        self._factory_argument = factory_argument
        self._config = config or ActivityConfig(
            start_to_close_timeout=timedelta(minutes=1)
        )
        self._declaration = declaration

    def _get_declaration(self) -> types.FunctionDeclaration | None:
        return self._declaration

    async def run_async(
        self, *, args: dict[str, Any], tool_context: ToolContext
    ) -> Any:
        result: _CallToolResult = await workflow.execute_activity(
            self._set_name + "-call-tool",
            _CallToolArguments(
                self._factory_argument,
                self.name,
                arguments=args,
                tool_context=TemporalToolContext(
                    tool_confirmation=tool_context.tool_confirmation,
                    function_call_id=tool_context.function_call_id,
                    event_actions=tool_context._event_actions,
                ),
            ),
            result_type=_CallToolResult,
            **self._config,
        )

        # We need to propagate any event actions back to the main context
        tool_context._event_actions = result.tool_context.event_actions
        return result.result


class TemporalMcpToolSet(BaseToolset):
    """Temporal-aware MCP toolset implementation.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Executes MCP tools as Temporal activities, providing proper isolation
    and execution guarantees within workflows.
    """

    def __init__(
        self,
        name: str,
        config: ActivityConfig | None = None,
        factory_argument: Any | None = None,
        not_in_workflow_toolset: Callable[[Any | None], McpToolset] | None = None,
    ):
        """Initializes the Temporal MCP toolset.

        Args:
            name: Name of the toolset (used for activity naming).
            config: Optional activity configuration.
            factory_argument: Optional argument passed to toolset factory.
            not_in_workflow_toolset: Optional factory that returns the
                underlying ``McpToolset`` to use when this wrapper executes
                outside ``workflow.in_workflow()``, such as local ADK runs.
                This is not needed during normal workflow execution, but
                ``get_tools()`` raises ``ValueError`` outside a workflow if it
                is omitted.
        """
        super().__init__()
        self._name = name
        self._factory_argument = factory_argument
        self._config = config or ActivityConfig(
            start_to_close_timeout=timedelta(minutes=1)
        )
        self._not_in_workflow_toolset = not_in_workflow_toolset

    async def get_tools(
        self, readonly_context: ReadonlyContext | None = None
    ) -> list[BaseTool]:
        """Retrieves available tools from the MCP toolset.

        Args:
            readonly_context: Optional readonly context (unused in this implementation).

        Returns:
            List of available tools wrapped as Temporal activities.
        """
        # If executed outside a workflow, like when doing local adk runs, use the mcp server directly
        if not workflow.in_workflow():
            if self._not_in_workflow_toolset is None:
                raise ValueError(
                    "Attempted to use TemporalMcpToolSet outside a workflow, but "
                    "no not_in_workflow_toolset was provided. Either use "
                    "McpToolSet directly or pass a factory that returns the "
                    "underlying McpToolset for non-workflow execution."
                )
            return await self._not_in_workflow_toolset(None).get_tools(readonly_context)

        tool_results: list[_ToolResult] = await workflow.execute_activity(
            self._name + "-list-tools",
            _GetToolsArguments(self._factory_argument),
            result_type=list[_ToolResult],
            **self._config,
        )
        return [
            _TemporalTool(
                set_name=self._name,
                factory_argument=self._factory_argument,
                config=self._config,
                declaration=tool_result.function_declaration,
                name=tool_result.name,
                description=tool_result.description,
                is_long_running=tool_result.is_long_running,
                custom_metadata=tool_result.custom_metadata,
            )
            for tool_result in tool_results
        ]
