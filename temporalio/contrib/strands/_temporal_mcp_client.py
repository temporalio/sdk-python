from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.types import PaginatedRequestParams, Tool
from strands.tools import ToolProvider
from strands.tools.mcp import MCPAgentTool, MCPClient
from strands.tools.mcp.mcp_types import MCPToolResult
from strands.types.tools import AgentTool

from temporalio import activity, workflow
from temporalio.common import Priority, RetryPolicy
from temporalio.workflow import ActivityCancellationType, VersioningIntent


@dataclass
class _MCPToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


@dataclass
class _CallToolArgs:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    tool_use_id: str = ""


class TemporalMCPClient(ToolProvider):
    """Workflow-side handle to an MCP server registered on the worker.

    The transport factory lives worker-side via
    ``StrandsPlugin(mcp_clients={"server": lambda: ...})``. This handle carries
    the server name (which selects the registered factory) and the per-call
    activity options. Tool discovery runs as the ``{server}-list-tools``
    activity, dispatched from inside the workflow by ``TemporalAgent`` before
    each model call.

    ``cache_tools`` controls how often that listing happens. When ``False``
    (the default) the tools are re-listed on every agent turn, so an MCP server
    restarted mid-workflow (with tools added, removed, or renamed) is picked up.
    When ``True`` the tools are listed once at the beginning of the workflow and
    reused for its lifetime.

    Construct once at module level and pass to ``TemporalAgent(tools=[...])``
    inside the workflow. Multiple handles may reference the same server name
    with different activity options.
    """

    def __init__(
        self,
        server: str,
        *,
        cache_tools: bool = False,
        task_queue: str | None = None,
        schedule_to_close_timeout: timedelta | None = None,
        schedule_to_start_timeout: timedelta | None = None,
        start_to_close_timeout: timedelta | None = None,
        heartbeat_timeout: timedelta | None = None,
        retry_policy: RetryPolicy | None = None,
        cancellation_type: ActivityCancellationType = ActivityCancellationType.TRY_CANCEL,
        versioning_intent: VersioningIntent | None = None,
        summary: str | None = None,
        priority: Priority = Priority.default,
    ) -> None:
        """Configure the server name and activity options."""
        self._server = server
        self._cache_tools = cache_tools
        self._tools: list[AgentTool] = []
        self._fetched = False
        self._options: dict[str, Any] = {
            "task_queue": task_queue,
            "schedule_to_close_timeout": schedule_to_close_timeout,
            "schedule_to_start_timeout": schedule_to_start_timeout,
            "start_to_close_timeout": start_to_close_timeout,
            "heartbeat_timeout": heartbeat_timeout,
            "retry_policy": retry_policy,
            "cancellation_type": cancellation_type,
            "versioning_intent": versioning_intent,
            "summary": summary,
            "priority": priority,
        }

    @property
    def server(self) -> str:
        """MCP server name used as the activity prefix."""
        return self._server

    async def load_tools(self, **_kwargs: Any) -> Sequence[AgentTool]:
        """Return the tools fetched by the most recent ``_refresh``.

        This must stay free of any ``workflow`` API: Strands invokes it once at
        ``Agent`` construction on a separate ``run_async`` thread that has no
        workflow runtime. ``TemporalAgent`` populates the tools by calling
        ``_refresh`` from a ``BeforeModelCallEvent`` hook before the registry is
        first read.
        """
        return list(self._tools)

    async def _refresh(self) -> None:
        """List the server's tools via the ``{server}-list-tools`` activity.

        Runs on the workflow event loop (dispatched from ``TemporalAgent``'s
        hook), so the activity result is recorded in history and replay-safe.
        """
        from ._temporal_mcp_tool import TemporalMCPTool

        infos: list[_MCPToolInfo] = await workflow.execute_activity(
            f"{self._server}-list-tools",
            result_type=list[_MCPToolInfo],
            **self._options,
        )
        self._tools = [
            TemporalMCPTool(self._server, info, self._options) for info in infos
        ]
        self._fetched = True

    def add_consumer(self, consumer_id: Any, **_kwargs: Any) -> None:
        """No-op; consumer tracking is handled by the underlying MCP client."""
        return None

    def remove_consumer(self, consumer_id: Any, **_kwargs: Any) -> None:
        """No-op; consumer tracking is handled by the underlying MCP client."""
        return None


# Use the MCP session directly instead of MCPClient's background-thread
# helpers. Those helpers route calls through cross-loop futures that are
# unreliable on Python 3.10 when invoked from Temporal's async worker/activity
# event loops.
async def _paginate_list_tools(session: ClientSession) -> list[Tool]:
    tools: list[Tool] = []
    pagination_token = None
    while True:
        page = await session.list_tools(
            params=PaginatedRequestParams(cursor=pagination_token)
            if pagination_token is not None
            else None
        )
        tools.extend(page.tools)
        pagination_token = page.nextCursor
        if pagination_token is None:
            return tools


def _tool_infos(client: MCPClient, tools: Sequence[Tool]) -> list[_MCPToolInfo]:
    """Apply the client's tool filters and project to serializable records."""
    infos: list[_MCPToolInfo] = []
    for tool in tools:
        if client._prefix:
            agent_tool = MCPAgentTool(
                tool, client, name_override=f"{client._prefix}_{tool.name}"
            )
        else:
            agent_tool = MCPAgentTool(tool, client)
        if not client._should_include_tool_with_filters(
            agent_tool, client._tool_filters
        ):
            continue
        infos.append(
            _MCPToolInfo(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema,
                output_schema=tool.outputSchema,
            )
        )
    return infos


# Default for how long an idle MCP connection stays open before it is
# disconnected. The timer resets on every call that reuses the connection.
# Override per worker via ``StrandsPlugin(mcp_connection_idle_timeout=...)``.
_MCP_CONNECTION_IDLE = timedelta(minutes=5)

# Server name -> live connection held open in the activity worker process.
# Activities run in the worker process , so this module state is shared across activity invocations on the worker
_CONNECTIONS: dict[str, _ConnectionRecord] = {}


class _ConnectionRecord:
    """A single MCP session held open by a dedicated owner task.

    The MCP transport and ``ClientSession`` are anyio context managers whose
    cancel scope is bound to the task that enters them, so they must be entered
    and exited in the same task. ``_run`` owns that task for the connection's
    whole lifetime; ``call_tool`` activities on the same event loop invoke
    ``session.call_tool`` directly (MCP multiplexes concurrent requests by id).
    """

    def __init__(
        self,
        server: str,
        client_factory: Callable[[], MCPClient],
        idle_timeout: timedelta,
    ) -> None:
        loop = asyncio.get_running_loop()
        self._server = server
        self._idle_timeout = idle_timeout
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[tuple[MCPClient, ClientSession]] = (
            loop.create_future()
        )
        self._idle_handle: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._inflight = 0
        self._owner = asyncio.create_task(self._run(client_factory))

    async def _run(self, client_factory: Callable[[], MCPClient]) -> None:
        client = client_factory()
        try:
            async with client._transport_callable() as (read_stream, write_stream, *_):
                async with ClientSession(
                    read_stream,
                    write_stream,
                    elicitation_callback=client._elicitation_callback,
                ) as session:
                    await session.initialize()
                    self._ready.set_result((client, session))
                    await self._stop.wait()
        except BaseException as err:
            # A failed connect should not be cached; drop it so the next call
            # retries instead of awaiting a permanently rejected future.
            if not self._ready.done():
                self._ready.set_exception(err)
            _CONNECTIONS.pop(self._server, None)
            raise

    def acquire(self) -> None:
        """Mark a call in flight; pause idle eviction while calls are active."""
        self._inflight += 1
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def release(self) -> None:
        """Mark a call done; arm idle eviction once no calls remain in flight."""
        self._inflight -= 1
        # Only the record still cached under this server arms a timer; a record
        # already evicted or never cached must not schedule one, or it could
        # later evict a different, healthy connection for the same server.
        if self._inflight == 0 and _CONNECTIONS.get(self._server) is self:
            loop = asyncio.get_running_loop()
            self._idle_handle = loop.call_later(
                self._idle_timeout.total_seconds(), self._on_idle
            )

    def _on_idle(self) -> None:
        self._idle_task = asyncio.ensure_future(self._maybe_evict())

    async def _maybe_evict(self) -> None:
        # A call may have acquired the connection between the timer firing and
        # this task running; only evict if it is still idle.
        if self._inflight == 0:
            await _evict_connection(self._server)

    async def aclose(self) -> None:
        """Signal the owner task to exit its context managers and wait for it."""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        self._stop.set()
        try:
            await self._owner
        except BaseException:
            pass

    async def session(self) -> tuple[MCPClient, ClientSession]:
        """Return the live client and session, or raise the connect failure."""
        return await self._ready


async def get_connection(
    server: str, client_factory: Callable[[], MCPClient], idle_timeout: timedelta
) -> tuple[MCPClient, ClientSession, _ConnectionRecord]:
    """Return the cached session for ``server``, opening one lazily if needed.

    Concurrent first-callers dedupe onto a single connect handshake by awaiting
    the same record. The returned record is acquired; the caller must
    ``release()`` it once the call completes so idle eviction can resume.
    """
    record = _CONNECTIONS.get(server)
    if record is None:
        record = _ConnectionRecord(server, client_factory, idle_timeout)
        _CONNECTIONS[server] = record
    record.acquire()
    try:
        client, session = await record.session()
    except BaseException:
        record.release()
        raise
    return client, session, record


async def _evict_connection(server: str) -> None:
    record = _CONNECTIONS.pop(server, None)
    if record is not None:
        await record.aclose()


def build_call_tool_activity(
    server: str,
    client_factory: Callable[[], MCPClient],
    idle_timeout: timedelta | None = None,
) -> Callable:
    """Return the per-server ``{server}-call-tool`` activity for registration.

    Reuses a worker-process MCP session opened lazily through ``client_factory``.
    Idle connections are disconnected after ``idle_timeout`` (defaults to
    ``_MCP_CONNECTION_IDLE``).
    """
    idle = idle_timeout if idle_timeout is not None else _MCP_CONNECTION_IDLE

    @activity.defn(name=f"{server}-call-tool")
    async def call_tool(args: _CallToolArgs) -> MCPToolResult:
        try:
            client, session, record = await get_connection(server, client_factory, idle)
        except Exception as err:
            # Connecting failed; map to a tool error result like a call would.
            return client_factory()._handle_tool_execution_error(args.tool_use_id, err)
        try:
            result = await session.call_tool(args.tool_name, args.arguments)
            return client._handle_tool_result(args.tool_use_id, result)
        except Exception as err:
            # The session may be broken; drop it so the next call reconnects.
            await _evict_connection(server)
            return client._handle_tool_execution_error(args.tool_use_id, err)
        finally:
            # No more in-flight call on this connection; let idle eviction
            # resume (no-op if the connection was just evicted above).
            record.release()

    return call_tool


def build_list_tools_activity(
    server: str,
    client_factory: Callable[[], MCPClient],
    idle_timeout: timedelta | None = None,
) -> Callable:
    """Return the per-server ``{server}-list-tools`` activity for registration.

    Lists the server's tools (applying the client's tool filters) and reuses
    the same lazily-opened, idle-evicted worker-process MCP session as
    ``{server}-call-tool``.
    """
    idle = idle_timeout if idle_timeout is not None else _MCP_CONNECTION_IDLE

    @activity.defn(name=f"{server}-list-tools")
    async def list_tools() -> list[_MCPToolInfo]:
        client, session, record = await get_connection(server, client_factory, idle)
        try:
            return _tool_infos(client, await _paginate_list_tools(session))
        except Exception:
            # The session may be broken; drop it so the next call reconnects.
            await _evict_connection(server)
            raise
        finally:
            record.release()

    return list_tools
