"""Temporal-aware ``AsyncClient``.

``TemporalAsyncClient`` is an ``AsyncClient`` whose every Gemini API call runs
as a Temporal activity.  It builds and wraps a private ``BaseApiClient`` that
dispatches HTTP through ``workflow.execute_activity`` instead of making network
calls, so the SDK's request-formatting code (including the AFC loop) runs in
the workflow while the real ``genai.Client`` with real credentials only exists
on the worker side inside the activity.

Construct it from within a workflow::

    client = TemporalAsyncClient(activity_config=...)
    response = await client.models.generate_content(...)

This ensures:

- No credential fetching or refreshing happens in the workflow.
- No auth material (tokens, API keys) appears in Temporal event history.
- The SDK's AFC (automatic function calling) loop runs in the workflow, so
  ``activity_as_tool()`` wrappers work naturally.

``AsyncFiles`` and ``AsyncFileSearchStores`` are replaced with shims that run
upload/download as activities; ``interactions`` and ``agents`` — which bypass
``BaseApiClient`` via a vendored HTTP client — are likewise replaced with
activity-backed shims; ``webhooks`` is not supported in workflows and raises.

Replay determinism
------------------
The SDK's request-formatting and automatic-function-calling loop run *in the
workflow*, so they must be deterministic.  A survey of ``google.genai`` found no
``time``/``uuid``/``random`` use on the ``generate_content``/AFC path; the SDK's
own non-deterministic code (HTTP retry backoff, the interactions/agents vendored
client, local tokenizer temp paths) runs only inside activities on the worker.
The SDK exposes no time/id provider hooks to override, and none are needed.

The one in-workflow exception is ``batches.create`` on Vertex AI: when
``display_name``/``dest`` are omitted the SDK auto-generates them from a
timestamp + UUID (``_common.timestamped_unique_name``), which is not
replay-safe.  Pass explicit ``display_name`` and ``dest`` when creating Vertex
batch jobs from a workflow.
"""

from __future__ import annotations

from datetime import timedelta
from typing import NoReturn

from google.genai._interactions.resources.agents import AsyncAgentsResource
from google.genai._interactions.resources.interactions import (
    AsyncInteractionsResource,
)
from google.genai.client import AsyncClient

from temporalio.contrib.google_genai._temporal_agents import (
    TemporalAsyncAgents,
)
from temporalio.contrib.google_genai._temporal_api_client import (
    _TemporalApiClient,
)
from temporalio.contrib.google_genai._temporal_file_search_stores import (
    TemporalAsyncFileSearchStores,
)
from temporalio.contrib.google_genai._temporal_files import (
    TemporalAsyncFiles,
)
from temporalio.contrib.google_genai._temporal_interactions import (
    TemporalAsyncInteractions,
)
from temporalio.workflow import ActivityConfig


class TemporalAsyncClient(AsyncClient):
    """An ``AsyncClient`` whose API calls run as Temporal activities.

    .. warning::
        This API is experimental and may change in future versions.
        Use with caution in production environments.

    Builds a private ``BaseApiClient`` that dispatches HTTP calls through
    ``workflow.execute_activity`` and wraps it, so the SDK's request-formatting
    code (including the AFC loop) runs in the workflow while only the actual
    API calls cross into activities.  Credentials are never fetched or stored
    in the workflow — the activity worker handles authentication independently.

    ``AsyncFiles`` and ``AsyncFileSearchStores`` are replaced with shims that
    run upload/download as activities; ``interactions`` and ``agents`` — which
    bypass ``BaseApiClient`` via a vendored HTTP client — are likewise replaced
    with activity-backed shims; ``webhooks`` is not supported in workflows and
    raises.  Other modules (models, tunings, caches, batches, live, tokens,
    operations) are inherited unchanged and work through the private api
    client's activity-backed HTTP methods.

    Construct it from within a workflow ``run`` method:

    .. code-block:: python

        @workflow.defn
        class MyWorkflow:
            @workflow.run
            async def run(self, query: str) -> str:
                client = TemporalAsyncClient()
                response = await client.models.generate_content(
                    model="gemini-2.0-flash",
                    contents=query,
                    config=GenerateContentConfig(
                        tools=[
                            activity_as_tool(
                                my_tool,
                                activity_config=ActivityConfig(
                                    start_to_close_timeout=timedelta(seconds=30),
                                ),
                            ),
                        ],
                    ),
                )
                return response.text
    """

    def __init__(
        self,
        *,
        vertexai: bool = False,
        project: str | None = None,
        location: str | None = None,
        activity_config: ActivityConfig | None = None,
        streaming_topic: str | None = None,
        streaming_batch_interval: timedelta = timedelta(milliseconds=100),
    ) -> None:
        """Initialize a Temporal-aware client.

        Args:
            vertexai: Whether to use Vertex AI API endpoints.  Must match the
                ``GoogleGenAIPlugin`` configuration on the worker side.
                Defaults to ``False`` (Gemini Developer API).
            project: Google Cloud project ID.  Only needed when
                ``vertexai=True`` and the SDK's request formatting requires it
                (e.g., cache operations).
            location: Google Cloud location.  Same conditions as ``project``.
            activity_config: Override the default activity configuration
                (timeouts, retry policy, etc.) for Gemini API call activities.
                When not provided, every operation (model calls, files,
                interactions, managed agents) defaults to a 60-second
                ``start_to_close_timeout`` and Temporal's default retry policy.
            streaming_topic: When set, ``generate_content_stream`` publishes each
                streamed ``GenerateContentResponse`` to this
                :class:`~temporalio.contrib.workflow_streams.WorkflowStream`
                topic as it arrives, so external consumers can observe the model
                output in real time.  The workflow must construct a
                ``WorkflowStream`` in ``@workflow.init``; otherwise the call
                raises.  The workflow's own stream iteration is unchanged.
            streaming_batch_interval: How often the streaming activity flushes
                published chunks to the workflow stream.  Defaults to 100ms.
        """
        api_client = _TemporalApiClient(
            vertexai=vertexai,
            project=project,
            location=location,
            activity_config=activity_config,
            streaming_topic=streaming_topic,
            streaming_batch_interval=streaming_batch_interval,
        )
        super().__init__(api_client)
        self._files = TemporalAsyncFiles(api_client, activity_config)
        self._file_search_stores = TemporalAsyncFileSearchStores(
            api_client, activity_config
        )
        self._temporal_interactions = TemporalAsyncInteractions(activity_config)
        self._temporal_agents = TemporalAsyncAgents(activity_config)

    @property
    def interactions(self) -> AsyncInteractionsResource:
        """Temporal-aware interactions resource; operations run as activities."""
        return self._temporal_interactions

    @property
    def agents(self) -> AsyncAgentsResource:
        """Temporal-aware agents resource; operations run as activities."""
        return self._temporal_agents

    @property
    def webhooks(self) -> NoReturn:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Raise — webhooks are not supported in Temporal workflows."""
        raise RuntimeError(
            "client.webhooks is not supported in Temporal workflows. "
            "Manage webhooks outside the workflow with a regular genai.Client."
        )
