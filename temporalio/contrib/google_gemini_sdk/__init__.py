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

- :class:`GeminiPlugin` — registers the ``gemini_api_client_async_request``
  activity using a caller-provided ``genai.Client`` on the worker side.
- :func:`gemini_client` — call from a workflow to get an ``AsyncClient``
  that routes API calls through activities.
- :func:`activity_as_tool` — convert any ``@activity.defn`` function into a
  Gemini tool callable; Gemini's AFC invokes it as a Temporal activity.

Quickstart::

    # ---- worker setup (outside the Temporal Python Sandbox) ----
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    plugin = GeminiPlugin(client)

    @activity.defn
    async def get_weather(state: str) -> str: ...

    # ---- workflow (inside the Temporal Python Sandbox) ----
    @workflow.defn
    class AgentWorkflow:
        @workflow.run
        async def run(self, query: str) -> str:
            client = gemini_client()
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

from temporalio.contrib.google_gemini_sdk._gemini_plugin import GeminiPlugin
from temporalio.contrib.google_gemini_sdk.workflow import (
    activity_as_tool,
    gemini_client,
)

__all__ = [
    "GeminiPlugin",
    "activity_as_tool",
    "gemini_client",
]
