"""First-class Temporal integration for the Google Gemini SDK.

.. warning::
    This module is experimental and may change in future versions.
    Use with caution in production environments.

This integration lets you use the Gemini SDK's async client with full
automatic function calling (AFC) support. Every API call becomes a
**durable Temporal activity**. Tools default to plain workflow methods
that run deterministically in-workflow; wrap any ``@activity.defn`` with
:func:`activity_as_tool` to run a tool as a durable activity instead.

No credentials are fetched in the workflow, and no auth material appears in
Temporal's event history.

- :class:`GoogleGenAIPlugin` — registers the Gemini SDK activities using a
  caller-provided ``genai.Client`` on the worker side.
- :class:`TemporalAsyncClient` — construct from a workflow to get an
  ``AsyncClient`` that routes API calls through activities.
- :func:`activity_as_tool` — convert any ``@activity.defn`` function into a
  Gemini tool callable; Gemini's AFC invokes it as a Temporal activity.

The Interactions API (``client.interactions``) and managed agents
(``client.agents``) are supported as whole-operation activities; streamed
interactions are batched (the activity drains the SSE stream and the
workflow iterates the collected events).  ``client.webhooks`` is not
supported in workflows.  The Interactions API has no automatic function
calling: declare tools as ``{"type": "function", ...}`` dicts (per the
Gemini docs) and drive the tool loop yourself, executing each call via
``workflow.execute_activity`` or an :func:`activity_as_tool` callable.

MCP is supported across three paths.  Client-side MCP (Gemini Developer API)
uses :class:`TemporalMcpClientSession`: register a server with
``GoogleGenAIPlugin(mcp_servers={name: factory})`` on the worker, then place
``TemporalMcpClientSession(name)`` in a ``generate_content`` ``tools`` list —
the SDK's AFC loop drives it, with ``list_tools`` / ``call_tool`` running as
activities against a pooled worker-side connection.  Server-side MCP on Vertex
AI (``Tool(mcp_servers=[McpServer(...)])``) and the Interactions API's MCP step
types are executed by Google's backend and flow through unchanged as request /
response data — no extra wiring needed.  Client-side MCP requires the ``mcp``
package.

Quickstart::

    # ---- worker setup (outside the Temporal Python Sandbox) ----
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    plugin = GoogleGenAIPlugin(client)

    @activity.defn
    async def get_weather(state: str) -> str: ...

    # ---- workflow (inside the Temporal Python Sandbox) ----
    @workflow.defn
    class AgentWorkflow:
        @workflow.run
        async def run(self, query: str) -> str:
            client = TemporalAsyncClient()
            response = await client.models.generate_content(
                model="gemini-2.5-flash",
                contents=query,
                config=types.GenerateContentConfig(
                    tools=[
                        activity_as_tool(
                            get_weather,
                            activity_config=ActivityConfig(
                                start_to_close_timeout=timedelta(seconds=30),
                            ),
                        ),
                    ],
                ),
            )
            return response.text
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from temporalio.contrib.google_genai._google_genai_plugin import GoogleGenAIPlugin
from temporalio.contrib.google_genai._temporal_async_client import (
    TemporalAsyncClient,
)
from temporalio.contrib.google_genai.workflow import (
    activity_as_tool,
)

if TYPE_CHECKING:
    from temporalio.contrib.google_genai._temporal_mcp import TemporalMcpClientSession

__all__ = [
    "GoogleGenAIPlugin",
    "TemporalAsyncClient",
    "TemporalMcpClientSession",
    "activity_as_tool",
]


def __getattr__(name: str) -> Any:
    """Lazily expose ``TemporalMcpClientSession`` without importing ``mcp`` eagerly.

    ``mcp`` is an optional dependency, so importing this package must not require
    it; the import (and any resulting ``ImportError``) is deferred until the
    symbol is actually accessed.
    """
    if name == "TemporalMcpClientSession":
        from temporalio.contrib.google_genai._temporal_mcp import (
            TemporalMcpClientSession,
        )

        return TemporalMcpClientSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
