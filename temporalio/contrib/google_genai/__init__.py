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

- :class:`GoogleGenAIPlugin` — registers the ``gemini_api_client_async_request``
  activity using a caller-provided ``genai.Client`` on the worker side.
- :func:`google_genai_client` — call from a workflow to get an ``AsyncClient``
  that routes API calls through activities.
- :func:`activity_as_tool` — convert any ``@activity.defn`` function into a
  Gemini tool callable; Gemini's AFC invokes it as a Temporal activity.

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
            client = google_genai_client()
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

from temporalio.contrib.google_genai._google_genai_plugin import GoogleGenAIPlugin
from temporalio.contrib.google_genai.workflow import (
    activity_as_tool,
    google_genai_client,
)

__all__ = [
    "GoogleGenAIPlugin",
    "activity_as_tool",
    "google_genai_client",
]
