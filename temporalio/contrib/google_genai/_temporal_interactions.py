"""Temporal-aware AsyncInteractionsResource shim.

``TemporalAsyncInteractions`` is an ``AsyncInteractionsResource`` subclass
whose methods dispatch through Temporal activities.  The Interactions API
does not go through ``BaseApiClient`` — it uses a vendored, Stainless-
generated HTTP client (``google.genai._interactions``) that the
``TemporalApiClient`` shim never sees — so each operation is routed as a
whole through an activity holding the real ``genai.Client`` on the worker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from types import TracebackType
from typing import Any, cast

from google.genai._interactions import AsyncStream
from google.genai._interactions._models import construct_type
from google.genai._interactions.resources.interactions import (
    AsyncInteractionsResource,
)
from google.genai._interactions.types import Interaction, InteractionSSEEvent

from temporalio import workflow as temporal_workflow
from temporalio.contrib.google_genai._models import (
    _GeminiInteractionIdRequest,
    _GeminiInteractionRequest,
    _GeminiInteractionStreamedResponse,
)
from temporalio.workflow import ActivityConfig

_DEFAULT_INTERACTION_TIMEOUT = timedelta(seconds=60)


def _deserialize(value: dict[str, Any], type_: Any) -> Any:
    """Rehydrate a dict into its concrete Stainless model.

    Uses the SDK's own ``construct_type`` rather than Pydantic's strict
    ``model_validate`` for two reasons: it dispatches discriminated unions
    on their ``event_type``-style discriminators (which plain Pydantic
    validation ignores for Stainless unions), and it tolerates the sparse
    nested payloads the API legitimately emits (e.g. an
    ``interaction.created`` event carries an ``Interaction`` with just
    ``id`` and ``object``).  It is a pure function, so it is safe to run
    in the workflow on every replay.
    """
    return construct_type(type_=type_, value=value)


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


class _TemporalInteractionAsyncStream(AsyncStream[InteractionSSEEvent]):
    """``AsyncStream`` replacement over events already drained in an activity.

    Iteration walks the in-memory event list, rehydrating each event back
    into its typed form.  Skips ``super().__init__`` (there is no httpx
    response or client on the workflow side), so ``close()`` and the
    context-manager methods are overridden as no-ops and the SDK stream's
    ``response`` attribute is not available.
    """

    def __init__(  # pyright: ignore[reportMissingSuperCall]
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


class TemporalAsyncInteractions(AsyncInteractionsResource):
    """``AsyncInteractionsResource`` subclass that routes calls through activities.

    Methods accept the same keyword arguments as the real resource and
    forward them verbatim — the SDK validates them on the worker side, so
    a bad argument surfaces as an activity failure (retried per the
    activity's retry policy) rather than a workflow-side error.

    ``with_raw_response`` / ``with_streaming_response`` are not supported
    in workflows.
    """

    def __init__(  # pyright: ignore[reportMissingSuperCall]
        self,
        activity_config: ActivityConfig | None = None,
    ) -> None:
        """Initialize without calling super (no real HTTP client exists here).

        ``super().__init__`` requires the vendored Stainless client, whose
        construction reads environment variables and builds httpx clients —
        none of which is allowed in the workflow sandbox.
        """
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

    async def create(  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> Interaction | AsyncStream[InteractionSSEEvent]:
        """Create an interaction via a Temporal activity.

        ``kwargs`` is forwarded verbatim to ``client.aio.interactions.create``
        on the worker.  With ``stream=True`` the activity drains the SSE
        stream and returns all events batched; the returned object supports
        ``async for`` / ``async with`` like the SDK's ``AsyncStream``.
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

    async def get(  # type: ignore[override]  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        id: str,
        *,
        stream: bool = False,
        **kwargs: Any,
    ) -> Interaction | AsyncStream[InteractionSSEEvent]:
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

    async def delete(  # pyright: ignore[reportIncompatibleMethodOverride]
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

    async def cancel(  # pyright: ignore[reportIncompatibleMethodOverride]
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
    def with_raw_response(self) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Raise — raw responses are not available in workflows."""
        raise RuntimeError("with_raw_response is not supported in Temporal workflows.")

    @property
    def with_streaming_response(self) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        """Raise — streaming responses are not available in workflows."""
        raise RuntimeError(
            "with_streaming_response is not supported in Temporal workflows."
        )
