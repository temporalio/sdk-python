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

import functools
import inspect
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any, NoReturn, cast

from google.genai import types
from google.genai.client import AsyncClient
from google.genai.models import AsyncModels

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


def _closure_if_bound_method(tool: object) -> object:
    """Return a plain-function wrapper for a bound method, else ``tool`` as-is.

    google-genai >= 2.8.0 deep-copies the request config internally
    (``config.model_copy(deep=True)``).  ``copy.deepcopy`` clones a bound
    method's ``__self__``, so a workflow-method tool would run against a throwaway
    clone of the workflow instance and its in-workflow state mutation would be
    lost.  ``deepcopy`` leaves plain functions untouched, so wrapping the method
    in a closure keeps the tool bound to the real instance.  Plain functions and
    ``activity_as_tool`` wrappers are already closures and pass through unchanged.
    """
    if not inspect.ismethod(tool):
        return tool
    method: Callable = tool
    if inspect.iscoroutinefunction(method):

        @functools.wraps(method)
        async def async_wrapper(*args: object, **kwargs: object) -> object:
            return await method(*args, **kwargs)

        return async_wrapper

    @functools.wraps(method)
    def sync_wrapper(*args: object, **kwargs: object) -> object:
        return method(*args, **kwargs)

    return sync_wrapper


def _wrap_bound_method_tools(
    config: types.GenerateContentConfigOrDict | None,
) -> types.GenerateContentConfigOrDict | None:
    """Closure-wrap any bound-method tools in ``config`` (see :func:`_closure_if_bound_method`).

    Returns ``config`` unchanged when it holds no bound-method tools; otherwise
    returns a shallow copy with the tools list rewritten, so the caller's config
    is never mutated.
    """
    if not config:
        return config
    if isinstance(config, dict):
        tools = config.get("tools")
        if not tools or not any(inspect.ismethod(t) for t in tools):
            return config
        updated: Any = {
            **config,
            "tools": [_closure_if_bound_method(t) for t in tools],
        }
        return cast(types.GenerateContentConfigDict, updated)
    if not config.tools or not any(inspect.ismethod(t) for t in config.tools):
        return config
    return config.model_copy(
        update={"tools": [_closure_if_bound_method(t) for t in config.tools]}
    )


class _TemporalAsyncModels(AsyncModels):
    """``AsyncModels`` that closure-wraps bound-method tools before each call.

    This shields workflow-method tools from google-genai's internal deep-copy of
    the config (>= 2.8.0), which would otherwise clone the workflow instance and
    silently drop the tool's in-workflow state mutations.
    """

    async def generate_content(  # type: ignore[override]
        self,
        *,
        model: str,
        contents: types.ContentListUnion | types.ContentListUnionDict,
        config: types.GenerateContentConfigOrDict | None = None,
    ) -> types.GenerateContentResponse:
        return await super().generate_content(
            model=model,
            contents=contents,
            config=_wrap_bound_method_tools(config),
        )

    async def generate_content_stream(  # type: ignore[override]
        self,
        *,
        model: str,
        contents: types.ContentListUnion | types.ContentListUnionDict,
        config: types.GenerateContentConfigOrDict | None = None,
    ) -> AsyncIterator[types.GenerateContentResponse]:
        return await super().generate_content_stream(
            model=model,
            contents=contents,
            config=_wrap_bound_method_tools(config),
        )


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
                :class:`temporalio.contrib.workflow_streams.WorkflowStream`
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
        # Closure-wrap bound-method tools so google-genai's internal
        # config deep-copy (>= 2.8.0) can't clone the workflow instance.
        self._models = _TemporalAsyncModels(api_client)
        self._files = TemporalAsyncFiles(api_client, activity_config)
        self._file_search_stores = TemporalAsyncFileSearchStores(
            api_client, activity_config
        )
        self._temporal_interactions = TemporalAsyncInteractions(activity_config)
        self._temporal_agents = TemporalAsyncAgents(activity_config)

    @property
    def interactions(  # type: ignore[override]
        self,
    ) -> TemporalAsyncInteractions:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Temporal-aware interactions resource; operations run as activities."""
        return self._temporal_interactions

    @property
    def agents(  # type: ignore[override]
        self,
    ) -> TemporalAsyncAgents:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Temporal-aware agents resource; operations run as activities."""
        return self._temporal_agents

    @property
    def webhooks(self) -> NoReturn:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Raise — webhooks are not supported in Temporal workflows."""
        raise RuntimeError(
            "client.webhooks is not supported in Temporal workflows. "
            "Manage webhooks outside the workflow with a regular genai.Client."
        )
