"""Temporal-aware agents resource shim.

``TemporalAsyncAgents`` exposes the same surface as google-genai's
``AsyncClient.agents`` resource, but each operation is dispatched through a
Temporal activity holding the real ``genai.Client`` on the worker.  Agents are
server-side managed agent definitions (created once, then referenced by id in
``interactions.create(agent=...)``); like the Interactions API, the resource
lives in the vendored Stainless client that bypasses ``BaseApiClient``, so each
operation is routed as a whole through an activity instead.

The shim depends only on the public ``google.genai.interactions`` surface, not
on google-genai internals.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

# These types are runtime-public (in ``google.genai.interactions.__all__``) but
# pyright's stubs don't mark them re-exported; the alternative it suggests is a
# private ``_gaos`` path, so suppress the false positive.
from google.genai.interactions import (
    Agent,  # pyright: ignore[reportPrivateImportUsage]
    AgentDeleteResponse,  # pyright: ignore[reportPrivateImportUsage]
    AgentListResponse,  # pyright: ignore[reportPrivateImportUsage]
)

from temporalio import workflow as temporal_workflow
from temporalio.contrib.google_genai._models import (
    _GeminiInteractionIdRequest,
    _GeminiInteractionRequest,
)
from temporalio.contrib.google_genai._temporal_interactions import (
    _deserialize,
    _pop_timeout,
)
from temporalio.workflow import ActivityConfig


class TemporalAsyncAgents:
    """Agents resource shim that routes calls through activities.

    Methods accept the same keyword arguments as the real resource and
    forward them verbatim — the SDK validates them on the worker side, so
    a bad argument surfaces as an activity failure (retried per the
    activity's retry policy) rather than a workflow-side error.

    ``with_raw_response`` / ``with_streaming_response`` are not supported
    in workflows.
    """

    def __init__(
        self,
        activity_config: ActivityConfig | None = None,
    ) -> None:
        self._activity_config = (
            ActivityConfig(start_to_close_timeout=timedelta(seconds=60))
            if activity_config is None
            else activity_config
        )

    def _config(self, summary: str, params: dict[str, Any]) -> ActivityConfig:
        config: ActivityConfig = {**self._activity_config}
        if "summary" not in config:
            config["summary"] = summary
        _pop_timeout(params, config)
        return config

    async def create(
        self,
        **kwargs: Any,
    ) -> Agent:
        """Create a managed agent definition via a Temporal activity."""
        params = dict(kwargs)
        config = self._config("agents.create", params)
        raw = await temporal_workflow.execute_activity(
            "gemini_agents_create",
            _GeminiInteractionRequest(params=params),
            result_type=dict[str, Any],
            **config,
        )
        return cast(Agent, _deserialize(raw, Agent))

    async def list(
        self,
        **kwargs: Any,
    ) -> AgentListResponse:
        """List managed agent definitions via a Temporal activity."""
        params = dict(kwargs)
        config = self._config("agents.list", params)
        raw = await temporal_workflow.execute_activity(
            "gemini_agents_list",
            _GeminiInteractionRequest(params=params),
            result_type=dict[str, Any],
            **config,
        )
        return cast(AgentListResponse, _deserialize(raw, AgentListResponse))

    async def get(
        self,
        id: str,
        **kwargs: Any,
    ) -> Agent:
        """Get a managed agent definition via a Temporal activity."""
        params = dict(kwargs)
        config = self._config("agents.get", params)
        raw = await temporal_workflow.execute_activity(
            "gemini_agents_get",
            _GeminiInteractionIdRequest(id=id, params=params),
            result_type=dict[str, Any],
            **config,
        )
        return cast(Agent, _deserialize(raw, Agent))

    async def delete(
        self,
        id: str,
        **kwargs: Any,
    ) -> AgentDeleteResponse:
        """Delete a managed agent definition via a Temporal activity."""
        params = dict(kwargs)
        config = self._config("agents.delete", params)
        raw = await temporal_workflow.execute_activity(
            "gemini_agents_delete",
            _GeminiInteractionIdRequest(id=id, params=params),
            result_type=dict[str, Any],
            **config,
        )
        return cast(AgentDeleteResponse, _deserialize(raw, AgentDeleteResponse))

    @property
    def with_raw_response(self) -> Any:
        """Raise — raw responses are not available in workflows."""
        raise RuntimeError("with_raw_response is not supported in Temporal workflows.")

    @property
    def with_streaming_response(self) -> Any:
        """Raise — streaming responses are not available in workflows."""
        raise RuntimeError(
            "with_streaming_response is not supported in Temporal workflows."
        )
