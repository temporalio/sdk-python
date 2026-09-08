"""Worker-side MCP activities and a pooled-connection subsystem.

The Gemini SDK's automatic-function-calling loop runs *inside* the workflow,
where it would otherwise call ``McpClientSession.list_tools`` /
``call_tool`` directly (network I/O — forbidden in a workflow).  The
workflow-side ``TemporalMcpClientSession`` shim redirects those two methods to
the ``{server}-list-tools`` / ``{server}-call-tool`` activities defined here,
so the real ``mcp.ClientSession`` lives only on the worker.

A single live session per server is held open in the worker process and reused
across activity invocations, with idle eviction — modeled on the strands
plugin's ``_temporal_mcp_client``.  The MCP transport and ``ClientSession`` are
anyio context managers whose cancel scope is bound to the task that enters
them, so a dedicated owner task (``_ConnectionRecord._run``) holds them open for
the connection's lifetime while concurrent activities on the same event loop
call through the shared session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import timedelta

from mcp import ClientSession
from mcp.types import CallToolResult, ListToolsResult

from temporalio import activity
from temporalio.contrib.google_genai._models import _McpCallToolRequest

# A factory yields a ready-to-use (connected and ``initialize()``-d)
# ``ClientSession`` as an async context manager.  Mirrors the strands
# ``mcp_clients={name: factory}`` shape; the user writes a small
# ``@asynccontextmanager`` that enters the transport, opens the session, and
# initializes it before ``yield``.
McpSessionFactory = Callable[[], AbstractAsyncContextManager[ClientSession]]

# Default time an idle MCP connection stays open before being disconnected.
# The timer resets on every call that reuses the connection.  Override per
# worker via ``GoogleGenAIPlugin(mcp_connection_idle_timeout=...)``.
_MCP_CONNECTION_IDLE = timedelta(minutes=5)

# Server name -> live connection held open in the activity worker process.
# Activities run in the worker process, so this module state is shared across
# activity invocations on the worker.
_CONNECTIONS: dict[str, _ConnectionRecord] = {}


class _ConnectionRecord:
    """A single MCP session held open by a dedicated owner task.

    ``_run`` enters and exits the session's context manager in the same task
    for the connection's whole lifetime; ``list_tools`` / ``call_tool``
    activities on the same event loop call through the shared session (MCP
    multiplexes concurrent requests by id).
    """

    def __init__(
        self,
        server: str,
        factory: McpSessionFactory,
        idle_timeout: timedelta,
    ) -> None:
        loop = asyncio.get_running_loop()
        self._server = server
        self._idle_timeout = idle_timeout
        self._stop = asyncio.Event()
        self._ready: asyncio.Future[ClientSession] = loop.create_future()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._inflight = 0
        self._owner = asyncio.create_task(self._run(factory))

    async def _run(self, factory: McpSessionFactory) -> None:
        try:
            async with factory() as session:
                self._ready.set_result(session)
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
        asyncio.ensure_future(self._maybe_evict())

    async def _maybe_evict(self) -> None:
        # A call may have acquired the connection between the timer firing and
        # this task running; only evict if it is still idle.
        if self._inflight == 0:
            await _evict_connection(self._server)

    async def aclose(self) -> None:
        """Signal the owner task to exit its context manager and wait for it."""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        self._stop.set()
        try:
            await self._owner
        except BaseException:
            pass

    async def session(self) -> ClientSession:
        """Return the live session, or raise the connect failure."""
        return await self._ready


async def get_connection(
    server: str, factory: McpSessionFactory, idle_timeout: timedelta
) -> tuple[ClientSession, _ConnectionRecord]:
    """Return the cached session for ``server``, opening one lazily if needed.

    Concurrent first-callers dedupe onto a single connect handshake by awaiting
    the same record.  The returned record is acquired; the caller must
    ``release()`` it once the call completes so idle eviction can resume.
    """
    record = _CONNECTIONS.get(server)
    if record is None:
        record = _ConnectionRecord(server, factory, idle_timeout)
        _CONNECTIONS[server] = record
    record.acquire()
    try:
        session = await record.session()
    except BaseException:
        record.release()
        raise
    return session, record


async def _evict_connection(server: str) -> None:
    record = _CONNECTIONS.pop(server, None)
    if record is not None:
        await record.aclose()


def build_list_tools_activity(
    server: str,
    factory: McpSessionFactory,
    idle_timeout: timedelta | None = None,
) -> Callable:
    """Return the per-server ``{server}-list-tools`` activity for registration.

    Reuses a lazily-opened, idle-evicted worker-process MCP session.  Returns
    the raw ``mcp.types.ListToolsResult`` so the workflow-side shim can hand it
    to the Gemini SDK exactly as a live session would (preserving the full tool
    parameter schema).
    """
    idle = idle_timeout if idle_timeout is not None else _MCP_CONNECTION_IDLE

    @activity.defn(name=f"{server}-list-tools")
    async def list_tools() -> ListToolsResult:
        session, record = await get_connection(server, factory, idle)
        try:
            return await session.list_tools()
        except Exception:
            # The session may be broken; drop it so the next call reconnects.
            await _evict_connection(server)
            raise
        finally:
            record.release()

    return list_tools


def build_call_tool_activity(
    server: str,
    factory: McpSessionFactory,
    idle_timeout: timedelta | None = None,
) -> Callable:
    """Return the per-server ``{server}-call-tool`` activity for registration.

    Reuses the same lazily-opened, idle-evicted worker-process MCP session as
    ``{server}-list-tools``.  Returns the raw ``mcp.types.CallToolResult`` —
    including tool-level error results (``isError=True``), which the model is
    meant to see; only transport/protocol failures raise (and evict).
    """
    idle = idle_timeout if idle_timeout is not None else _MCP_CONNECTION_IDLE

    @activity.defn(name=f"{server}-call-tool")
    async def call_tool(req: _McpCallToolRequest) -> CallToolResult:
        session, record = await get_connection(server, factory, idle)
        try:
            return await session.call_tool(name=req.name, arguments=req.arguments)
        except Exception:
            # The session may be broken; drop it so the next call reconnects.
            await _evict_connection(server)
            raise
        finally:
            record.release()

    return call_tool


def build_mcp_activities(
    mcp_servers: dict[str, McpSessionFactory],
    idle_timeout: timedelta | None = None,
) -> list[Callable]:
    """Build the list-tools and call-tool activities for every registered server."""
    activities: list[Callable] = []
    for server, factory in mcp_servers.items():
        activities.append(build_list_tools_activity(server, factory, idle_timeout))
        activities.append(build_call_tool_activity(server, factory, idle_timeout))
    return activities
