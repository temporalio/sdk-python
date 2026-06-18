"""Temporal-aware BaseApiClient that routes SDK calls through activities.

This module provides ``_TemporalApiClient``, a ``BaseApiClient`` subclass
whose HTTP methods dispatch through Temporal activities instead of making
direct calls.  The real ``genai.Client`` with real credentials only exists
on the worker side inside the activity.

This ensures:

- No credential fetching or refreshing happens in the workflow.
- No auth material (tokens, API keys) appears in Temporal event history.
- The SDK's AFC (automatic function calling) loop runs in the workflow,
  so ``activity_as_tool()`` wrappers work naturally.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from google.genai._api_client import BaseApiClient
from google.genai.types import HttpOptions, HttpOptionsOrDict
from google.genai.types import HttpResponse as SdkHttpResponse

from temporalio import workflow as temporal_workflow
from temporalio.contrib.google_genai._errors import GoogleGenAIError
from temporalio.contrib.google_genai._models import (
    _GeminiApiRequest,
    _GeminiApiResponse,
    _GeminiApiStreamedResponse,
    _SerializableHttpOptions,
)
from temporalio.workflow import ActivityConfig

# Fields on HttpOptions that cannot be serialized or should not be forwarded.
_REJECTED_HTTP_OPTION_FIELDS = frozenset(
    {
        "httpx_client",
        "httpx_async_client",
        "aiohttp_client",
        "client_args",
        "async_client_args",
    }
)


def _validate_http_options(http_options: HttpOptions | None) -> None:
    """Raise if http_options contains non-serializable fields."""
    if http_options is None:
        return
    bad_fields = [
        f
        for f in _REJECTED_HTTP_OPTION_FIELDS
        if getattr(http_options, f, None) is not None
    ]
    if bad_fields:
        raise ValueError(
            f"http_options cannot include {bad_fields}. "
            f"Configure custom HTTP clients at GoogleGenAIPlugin init instead."
        )


class _TemporalApiClient(BaseApiClient):
    """A ``BaseApiClient`` that routes all API calls through Temporal activities.

    This client is used on the workflow side. It does NOT initialize HTTP
    clients, load credentials, or make any network calls. It only holds the
    minimal configuration needed for the SDK's request formatting logic
    (e.g., choosing between Vertex AI and ML Dev parameter transformations).

    All actual HTTP calls are dispatched via ``workflow.execute_activity``.
    """

    def __init__(  # pyright: ignore[reportMissingSuperCall]
        self,
        *,
        vertexai: bool = False,
        project: str | None = None,
        location: str | None = None,
        activity_config: ActivityConfig | None = None,
    ) -> None:
        """Initialize without calling super (no HTTP clients needed)."""
        # Do NOT call super().__init__() — it creates HTTP clients, loads
        # credentials, etc.  We only set the properties the SDK's request
        # formatting code accesses.
        self.vertexai = vertexai
        self.project = project
        self.location = location
        self.api_key: str | None = None
        self.custom_base_url: str | None = None

        self._activity_config = (
            ActivityConfig(start_to_close_timeout=timedelta(seconds=60))
            if activity_config is None
            else activity_config
        )

    def _verify_response(self, response_model: Any) -> None:
        """No-op — matches the base implementation."""
        pass

    def close(self) -> None:
        """No-op — no HTTP resources to close."""
        pass

    async def aclose(self) -> None:
        """No-op — no HTTP resources to close."""
        pass

    def __del__(self) -> None:
        """No-op — no HTTP resources to clean up."""
        pass

    @staticmethod
    def _process_http_options(
        http_options: HttpOptionsOrDict | None,
        config: ActivityConfig,
    ) -> _SerializableHttpOptions | None:
        """Validate and extract serializable per-request HTTP options.

        Rejects non-serializable fields (custom HTTP clients), maps timeout
        to the Temporal activity config, and returns the remaining options
        for forwarding to the activity.

        Args:
            http_options: Per-request options from the SDK call.
            config: Mutable activity config dict — timeout is applied here.

        Returns:
            Serializable options to forward, or None if nothing to forward.
        """
        if http_options is None:
            return None

        if isinstance(http_options, HttpOptions):
            opts = http_options
        else:
            opts = HttpOptions.model_validate(http_options)

        _validate_http_options(opts)

        if opts.retry_options is not None:
            raise GoogleGenAIError(
                "Per-request http_options.retry_options is not supported in "
                "Temporal workflows. Temporal owns retries; configure them with "
                "the activity retry_policy via activity_config instead."
            )

        # timeout is owned by Temporal — apply it to the activity config
        # rather than forwarding to the underlying HTTP client.
        if opts.timeout is not None:
            config["start_to_close_timeout"] = timedelta(milliseconds=opts.timeout)

        result = _SerializableHttpOptions(
            base_url=opts.base_url,
            base_url_resource_scope=(
                opts.base_url_resource_scope.value
                if opts.base_url_resource_scope
                else None
            ),
            api_version=opts.api_version,
            headers=opts.headers,
            extra_body=opts.extra_body,
        )
        # Only return if there are actual values set
        if not result.model_dump(exclude_none=True):
            return None
        return result

    # ── Async (primary path for workflows) ──────────────────────────────

    async def async_request(
        self,
        http_method: str,
        path: str,
        request_dict: dict[str, object],
        http_options: HttpOptionsOrDict | None = None,
    ) -> SdkHttpResponse:
        """Dispatch an async API request through a Temporal activity."""
        config: ActivityConfig = {**self._activity_config}
        if "summary" not in config:
            # Default summary is the API path (e.g. "models/gemini-2.5-flash:generateContent").
            config["summary"] = f"{http_method.upper()} {path}"
        overrides = self._process_http_options(http_options, config)

        resp = await temporal_workflow.execute_activity(
            "gemini_api_client_async_request",
            _GeminiApiRequest(
                http_method=http_method,
                path=path,
                request_dict=request_dict,
                http_options_overrides=overrides,
            ),
            result_type=_GeminiApiResponse,
            **config,
        )
        return SdkHttpResponse(headers=resp.headers, body=resp.body)

    # ── Sync (not expected in async workflows, but raise clearly) ───────

    def request(
        self,
        http_method: str,
        path: str,
        request_dict: dict[str, object],
        http_options: HttpOptionsOrDict | None = None,
    ) -> SdkHttpResponse:
        """Raise — sync requests not supported in workflows."""
        raise RuntimeError(
            "Synchronous requests are not supported in Temporal workflows. "
            "Use TemporalAsyncClient instead."
        )

    def request_streamed(
        self,
        http_method: str,
        path: str,
        request_dict: dict[str, object],
        http_options: HttpOptionsOrDict | None = None,
    ) -> Any:
        """Raise — sync streaming not supported in workflows."""
        raise RuntimeError(
            "Synchronous streaming is not supported in Temporal workflows. "
            "Use TemporalAsyncClient instead."
        )

    async def async_request_streamed(
        self,
        http_method: str,
        path: str,
        request_dict: dict[str, object],
        http_options: HttpOptionsOrDict | None = None,
    ) -> Any:
        """Dispatch a streamed request, batching chunks in the activity."""
        config: ActivityConfig = {**self._activity_config}
        if "summary" not in config:
            config["summary"] = f"{http_method.upper()} {path}"
        overrides = self._process_http_options(http_options, config)

        resp = await temporal_workflow.execute_activity(
            "gemini_api_client_async_request_streamed",
            _GeminiApiRequest(
                http_method=http_method,
                path=path,
                request_dict=request_dict,
                http_options_overrides=overrides,
            ),
            result_type=_GeminiApiStreamedResponse,
            **config,
        )

        async def _yield_chunks():
            for chunk in resp.chunks:
                yield SdkHttpResponse(headers=chunk.headers, body=chunk.body)

        return _yield_chunks()

    # ── File upload/download ─────────────────────────────────────────────
    # File operations are handled at a higher level by TemporalAsyncFiles
    # (in _temporal_files.py), which dispatches the entire upload/download
    # as a Temporal activity using the real client on the worker side.
    # These internal BaseApiClient methods are not called in that path,
    # so we raise here to catch any unexpected direct usage.

    def upload_file(self, *args: Any, **kwargs: Any) -> Any:
        """Raise — use client.files.upload() instead."""
        raise NotImplementedError(
            "Use client.files.upload() instead of the internal upload_file() method."
        )

    async def async_upload_file(self, *args: Any, **kwargs: Any) -> Any:
        """Raise — use client.files.upload() instead."""
        raise NotImplementedError(
            "Use client.files.upload() instead of the internal async_upload_file() method."
        )

    def download_file(self, *args: Any, **kwargs: Any) -> Any:
        """Raise — use client.files.download() instead."""
        raise NotImplementedError(
            "Use client.files.download() instead of the internal download_file() method."
        )

    async def async_download_file(self, *args: Any, **kwargs: Any) -> Any:
        """Raise — use client.files.download() instead."""
        raise NotImplementedError(
            "Use client.files.download() instead of the internal async_download_file() method."
        )
