"""Temporal activity that executes Gemini SDK API calls with real credentials.

The ``TemporalApiClient`` in the workflow dispatches calls here. This
activity holds a user-provided ``genai.Client`` and forwards structured
requests. Credentials are fetched/refreshed only within the activity —
they never appear in workflow event history.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Callable

import google.auth.credentials
from google.genai import Client as GeminiClient
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.interactions import Interaction
from google.genai.types import HttpOptions
from google.genai.types import HttpResponse as SdkHttpResponse

from temporalio import activity
from temporalio.contrib.google_genai._models import (
    _GeminiApiRequest,
    _GeminiApiResponse,
    _GeminiApiStreamedResponse,
    _GeminiDownloadFileRequest,
    _GeminiInteractionIdRequest,
    _GeminiInteractionRequest,
    _GeminiInteractionStreamedResponse,
    _GeminiRegisterFilesRequest,
    _GeminiUploadFileRequest,
    _GeminiUploadToFileSearchStoreRequest,
)
from temporalio.contrib.workflow_streams import WorkflowStreamClient
from temporalio.exceptions import ApplicationError


def _resolve_http_options(
    overrides: Any,
) -> HttpOptions | None:
    """Reconstruct ``HttpOptions`` from serializable overrides, or None."""
    if overrides is None:
        return None
    return HttpOptions.model_validate(overrides.model_dump(exclude_none=True))


# HTTP status codes the Gemini SDK itself treats as transient/retryable.
_RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _classify_api_error(err: genai_errors.APIError) -> ApplicationError:
    """Map a Gemini ``APIError`` to an ``ApplicationError`` Temporal can act on.

    Transient statuses (timeouts, rate limits, 5xx) stay retryable so the
    activity's retry policy applies; everything else (e.g. 4xx client errors)
    is marked non-retryable so the workflow fails fast instead of retrying a
    request that cannot succeed.
    """
    code = getattr(err, "code", None)
    retryable = code in _RETRYABLE_HTTP_STATUS
    return ApplicationError(
        str(err),
        type=type(err).__name__,
        non_retryable=not retryable,
    )


async def _drain_interaction_stream(
    stream: Any,
) -> _GeminiInteractionStreamedResponse:
    """Collect every SSE event from an interaction stream, heartbeating per event.

    ``stream`` is the SDK's async streaming response; its concrete class is not a
    stable public name, so it is typed structurally — only ``async with`` /
    ``async for`` / ``event.model_dump(...)`` are used.
    """
    events: list[dict[str, Any]] = []
    async with stream:
        async for event in stream:
            activity.heartbeat()
            events.append(
                event.model_dump(by_alias=True, exclude_none=True, mode="json")
            )
    return _GeminiInteractionStreamedResponse(events=events)


class GeminiApiCaller:
    """Wraps a ``genai.Client`` and exposes Temporal activities for SDK calls.

    The caller owns a reference to the user-provided ``genai.Client``.
    All credential management, HTTP client configuration, etc. is the
    responsibility of whoever constructs the client.
    """

    def __init__(
        self,
        client: GeminiClient,
        credentials: google.auth.credentials.Credentials | None = None,
    ) -> None:
        """Initialize with a genai.Client and optional extra credentials."""
        self._client = client
        self._credentials = credentials

    def activities(self) -> Sequence[Callable]:
        """Return activities that route SDK calls through this client."""

        @activity.defn
        async def gemini_api_client_async_request(
            req: _GeminiApiRequest,
        ) -> _GeminiApiResponse:
            """Execute a Gemini SDK API call with real credentials."""
            try:
                response: SdkHttpResponse = (
                    await self._client.aio._api_client.async_request(
                        http_method=req.http_method,
                        path=req.path,
                        request_dict=req.request_dict,
                        http_options=_resolve_http_options(req.http_options_overrides),
                    )
                )
            except genai_errors.APIError as err:
                raise _classify_api_error(err) from err
            return _GeminiApiResponse(
                headers=response.headers or {},
                body=response.body or "",
            )

        @activity.defn
        async def gemini_api_client_async_request_streamed(
            req: _GeminiApiRequest,
        ) -> _GeminiApiStreamedResponse:
            """Execute a streamed Gemini SDK API call, collecting all chunks.

            When ``req.streaming_topic`` is set, each chunk is also published to
            that workflow-stream topic (parsed as ``GenerateContentResponse``)
            as it arrives, so external consumers see the model output in real
            time.  Chunks are still returned batched for the SDK to parse
            in-workflow; publishing is best-effort and never breaks that path.
            """
            chunks: list[_GeminiApiResponse] = []
            try:
                async with AsyncExitStack() as stack:
                    topic = None
                    if req.streaming_topic:
                        publisher = WorkflowStreamClient.from_within_activity(
                            batch_interval=timedelta(
                                milliseconds=req.streaming_batch_interval_ms
                            ),
                        )
                        await stack.enter_async_context(publisher)
                        topic = publisher.topic(
                            req.streaming_topic, type=types.GenerateContentResponse
                        )

                    stream = await self._client.aio._api_client.async_request_streamed(
                        http_method=req.http_method,
                        path=req.path,
                        request_dict=req.request_dict,
                        http_options=_resolve_http_options(req.http_options_overrides),
                    )
                    async for chunk in stream:
                        body = chunk.body or ""
                        chunks.append(
                            _GeminiApiResponse(headers=chunk.headers or {}, body=body)
                        )
                        if topic is not None and body:
                            try:
                                topic.publish(
                                    types.GenerateContentResponse.model_validate_json(
                                        body
                                    )
                                )
                            except Exception:
                                # Best-effort: a malformed/transform-needing chunk
                                # must not break the batched return.
                                pass
            except genai_errors.APIError as err:
                raise _classify_api_error(err) from err
            return _GeminiApiStreamedResponse(chunks=chunks)

        @activity.defn
        async def gemini_files_upload(
            req: _GeminiUploadFileRequest,
        ) -> types.File:
            """Upload a file using the real genai.Client on the worker."""
            if req.file_bytes is not None:
                import io

                file_arg: Any = io.BytesIO(req.file_bytes)
            else:
                file_arg = req.file_path

            return await self._client.aio.files.upload(file=file_arg, config=req.config)

        @activity.defn
        async def gemini_files_download(
            req: _GeminiDownloadFileRequest,
        ) -> bytes:
            """Download a file using the real genai.Client on the worker."""
            return await self._client.aio.files.download(
                file=req.file, config=req.config
            )

        @activity.defn
        async def gemini_files_register(
            req: _GeminiRegisterFilesRequest,
        ) -> types.RegisterFilesResponse:
            """Register GCS files using the real genai.Client on the worker.

            Uses ``credentials`` if provided at plugin init,
            otherwise falls back to the client's own credentials.
            Token refresh happens here on the worker side, so no auth
            material enters the workflow event history.
            """
            auth = self._credentials or self._client._api_client._credentials
            if auth is None:
                raise ValueError(
                    "No credentials available for register_files(). "
                    "Pass extra_credentials to GoogleGenAIPlugin or initialize "
                    "the genai.Client with credentials."
                )
            return await self._client.aio.files.register_files(
                auth=auth,
                uris=req.uris,
                config=req.config,
            )

        @activity.defn
        async def gemini_file_search_stores_upload(
            req: _GeminiUploadToFileSearchStoreRequest,
        ) -> types.UploadToFileSearchStoreOperation:
            """Upload a file to a file search store on the worker."""
            if req.file_bytes is not None:
                import io

                file_arg: Any = io.BytesIO(req.file_bytes)
            else:
                file_arg = req.file_path

            return (
                await self._client.aio.file_search_stores.upload_to_file_search_store(
                    file_search_store_name=req.file_search_store_name,
                    file=file_arg,
                    config=req.config,
                )
            )

        @activity.defn
        async def gemini_interactions_create(
            req: _GeminiInteractionRequest,
        ) -> dict[str, Any]:
            """Create an interaction using the real genai.Client on the worker."""
            interaction = await self._client.aio.interactions.create(**req.params)
            assert isinstance(interaction, Interaction)
            return interaction.model_dump(by_alias=True, exclude_none=True, mode="json")

        @activity.defn
        async def gemini_interactions_create_streamed(
            req: _GeminiInteractionRequest,
        ) -> _GeminiInteractionStreamedResponse:
            """Create a streamed interaction, collecting all SSE events."""
            stream = await self._client.aio.interactions.create(
                stream=True, **req.params
            )
            assert not isinstance(stream, Interaction)
            return await _drain_interaction_stream(stream)

        @activity.defn
        async def gemini_interactions_get(
            req: _GeminiInteractionIdRequest,
        ) -> dict[str, Any]:
            """Get an interaction using the real genai.Client on the worker."""
            interaction = await self._client.aio.interactions.get(req.id, **req.params)
            return interaction.model_dump(by_alias=True, exclude_none=True, mode="json")

        @activity.defn
        async def gemini_interactions_get_streamed(
            req: _GeminiInteractionIdRequest,
        ) -> _GeminiInteractionStreamedResponse:
            """Get a streamed interaction, collecting all SSE events."""
            stream = await self._client.aio.interactions.get(
                req.id, stream=True, **req.params
            )
            assert not isinstance(stream, Interaction)
            return await _drain_interaction_stream(stream)

        @activity.defn
        async def gemini_interactions_delete(
            req: _GeminiInteractionIdRequest,
        ) -> Any:
            """Delete an interaction using the real genai.Client on the worker."""
            return await self._client.aio.interactions.delete(req.id, **req.params)

        @activity.defn
        async def gemini_interactions_cancel(
            req: _GeminiInteractionIdRequest,
        ) -> dict[str, Any]:
            """Cancel an interaction using the real genai.Client on the worker."""
            interaction = await self._client.aio.interactions.cancel(
                req.id, **req.params
            )
            return interaction.model_dump(by_alias=True, exclude_none=True, mode="json")

        @activity.defn
        async def gemini_agents_create(
            req: _GeminiInteractionRequest,
        ) -> dict[str, Any]:
            """Create a managed agent using the real genai.Client on the worker."""
            agent = await self._client.aio.agents.create(**req.params)
            return agent.model_dump(by_alias=True, exclude_none=True, mode="json")

        @activity.defn
        async def gemini_agents_list(
            req: _GeminiInteractionRequest,
        ) -> dict[str, Any]:
            """List managed agents using the real genai.Client on the worker."""
            response = await self._client.aio.agents.list(**req.params)
            return response.model_dump(by_alias=True, exclude_none=True, mode="json")

        @activity.defn
        async def gemini_agents_get(
            req: _GeminiInteractionIdRequest,
        ) -> dict[str, Any]:
            """Get a managed agent using the real genai.Client on the worker."""
            agent = await self._client.aio.agents.get(req.id, **req.params)
            return agent.model_dump(by_alias=True, exclude_none=True, mode="json")

        @activity.defn
        async def gemini_agents_delete(
            req: _GeminiInteractionIdRequest,
        ) -> dict[str, Any]:
            """Delete a managed agent using the real genai.Client on the worker."""
            response = await self._client.aio.agents.delete(req.id, **req.params)
            return response.model_dump(by_alias=True, exclude_none=True, mode="json")

        return [
            gemini_api_client_async_request,
            gemini_api_client_async_request_streamed,
            gemini_files_upload,
            gemini_files_download,
            gemini_files_register,
            gemini_file_search_stores_upload,
            gemini_interactions_create,
            gemini_interactions_create_streamed,
            gemini_interactions_get,
            gemini_interactions_get_streamed,
            gemini_interactions_delete,
            gemini_interactions_cancel,
            gemini_agents_create,
            gemini_agents_list,
            gemini_agents_get,
            gemini_agents_delete,
        ]
