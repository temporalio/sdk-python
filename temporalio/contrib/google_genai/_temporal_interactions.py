"""Temporal-aware interactions resource shim.

``TemporalAsyncInteractions`` exposes the same surface as google-genai's
``AsyncClient.interactions`` resource, but each operation is dispatched through
a Temporal activity holding the real ``genai.Client`` on the worker.  The
Interactions API does not go through ``BaseApiClient`` — it uses a vendored,
Stainless-generated HTTP client that the ``TemporalApiClient`` shim never sees —
so each operation is routed as a whole through an activity instead.

The shim depends only on the public ``google.genai.interactions`` surface (types
plus ``client.aio.interactions`` on the worker), not on google-genai internals,
so it is unaffected by regeneration of the vendored client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from types import TracebackType
from typing import Any, cast

import pydantic
from google.genai.interactions import Interaction, InteractionSSEEvent

from temporalio import workflow as temporal_workflow
from temporalio.contrib.google_genai._models import (
    _GeminiInteractionIdRequest,
    _GeminiInteractionRequest,
    _GeminiInteractionStreamedResponse,
)
from temporalio.workflow import ActivityConfig

_DEFAULT_INTERACTION_TIMEOUT = timedelta(seconds=60)

# ``InteractionSSEEvent`` is a discriminated union (on ``event_type``); a
# ``TypeAdapter`` dispatches it — including nested unions such as a
# ``step.start`` event's ``step`` — leniently.
_SSE_EVENT_ADAPTER: pydantic.TypeAdapter[Any] = pydantic.TypeAdapter(
    InteractionSSEEvent
)


def _deserialize(value: dict[str, Any], type_: Any) -> Any:
    """Rehydrate a dict returned by an activity into its public genai model.

    ``InteractionSSEEvent`` is deserialized through its ``TypeAdapter``, which
    dispatches the discriminated union (and nested unions) and tolerates the
    sparse nested payloads the API legitimately emits (e.g. an
    ``interaction.created`` event carrying an ``Interaction`` with just ``id``
    and ``object``).  Plain models use ``model_validate``, which recurses nested
    models (e.g. ``AgentListResponse.agents`` into ``Agent``) and resolves
    aliases; the SDK's optional fields keep it tolerant of the minimal objects
    the API returns.  Both paths are pure functions, safe to run in the workflow
    on every replay.
    """
    if type_ is InteractionSSEEvent:
        return _SSE_EVENT_ADAPTER.validate_python(value)
    return type_.model_validate(value)


def _pop_timeout(params: dict[str, Any], config: ActivityConfig) -> None:
    """Pop a per-call ``timeout`` kwarg and apply it to the activity config.

    The Interactions API expresses timeouts in seconds.  Temporal owns
    timeouts/retries, so the value maps to ``start_to_close_timeout``
    rather than being forwarded to the underlying HTTP client.
    """
    timeout = params.pop("timeout", None)
    if timeout is None:
        return
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise ValueError(
            "timeout must be numeric seconds when calling the Interactions "
            "API from a workflow; configure anything more granular via "
            "activity_config instead."
        )
    config["start_to_close_timeout"] = timedelta(seconds=timeout)


class _TemporalInteractionAsyncStream:
    """Async stream over interaction events already drained in an activity.

    Presents the same ``async for`` / ``async with`` / ``close()`` surface as
    the SDK's streaming response, but iterates an in-memory event list drained
    inside the activity (there is no httpx response or client on the workflow
    side), rehydrating each event back into its typed form on iteration.
    """

    def __init__(
        self,
        events: list[dict[str, Any]],
    ) -> None:
        self._events = events

    def __aiter__(self) -> AsyncIterator[InteractionSSEEvent]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[InteractionSSEEvent]:
        for event in self._events:
            yield cast(InteractionSSEEvent, _deserialize(event, InteractionSSEEvent))

    async def __aenter__(self) -> _TemporalInteractionAsyncStream:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass

    async def close(self) -> None:
        """No-op — the upstream stream was drained inside the activity."""
        pass


class TemporalAsyncInteractions:
    """Interactions resource shim that routes calls through activities.

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
            ActivityConfig(start_to_close_timeout=_DEFAULT_INTERACTION_TIMEOUT)
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
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> Interaction | _TemporalInteractionAsyncStream:
        """Create an interaction via a Temporal activity.

        ``kwargs`` is forwarded verbatim to ``client.aio.interactions.create``
        on the worker.  With ``stream=True`` the activity drains the SSE
        stream and returns all events batched; the returned object supports
        ``async for`` / ``async with`` like the SDK's streaming response.
        """
        params = dict(kwargs)
        config = self._config(
            "interactions.create (stream)" if stream else "interactions.create",
            params,
        )
        req = _GeminiInteractionRequest(params=params)
        if stream:
            resp = await temporal_workflow.execute_activity(
                "gemini_interactions_create_streamed",
                req,
                result_type=_GeminiInteractionStreamedResponse,
                **config,
            )
            return _TemporalInteractionAsyncStream(resp.events)
        raw = await temporal_workflow.execute_activity(
            "gemini_interactions_create",
            req,
            result_type=dict[str, Any],
            **config,
        )
        return cast(Interaction, _deserialize(raw, Interaction))

    async def get(
        self,
        id: str,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> Interaction | _TemporalInteractionAsyncStream:
        """Get an interaction via a Temporal activity.

        Supports ``stream=True`` (with the SDK's ``last_event_id`` kwarg
        for resumption); events come back batched like :meth:`create`.
        """
        params = dict(kwargs)
        config = self._config(
            "interactions.get (stream)" if stream else "interactions.get",
            params,
        )
        req = _GeminiInteractionIdRequest(id=id, params=params)
        if stream:
            resp = await temporal_workflow.execute_activity(
                "gemini_interactions_get_streamed",
                req,
                result_type=_GeminiInteractionStreamedResponse,
                **config,
            )
            return _TemporalInteractionAsyncStream(resp.events)
        raw = await temporal_workflow.execute_activity(
            "gemini_interactions_get",
            req,
            result_type=dict[str, Any],
            **config,
        )
        return cast(Interaction, _deserialize(raw, Interaction))

    async def delete(
        self,
        id: str,
        **kwargs: Any,
    ) -> object:
        """Delete an interaction via a Temporal activity."""
        params = dict(kwargs)
        config = self._config("interactions.delete", params)
        return await temporal_workflow.execute_activity(
            "gemini_interactions_delete",
            _GeminiInteractionIdRequest(id=id, params=params),
            **config,
        )

    async def cancel(
        self,
        id: str,
        **kwargs: Any,
    ) -> Interaction:
        """Cancel an interaction via a Temporal activity."""
        params = dict(kwargs)
        config = self._config("interactions.cancel", params)
        raw = await temporal_workflow.execute_activity(
            "gemini_interactions_cancel",
            _GeminiInteractionIdRequest(id=id, params=params),
            result_type=dict[str, Any],
            **config,
        )
        return cast(Interaction, _deserialize(raw, Interaction))

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
