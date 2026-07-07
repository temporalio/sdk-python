"""Serializable Pydantic models for the Gemini SDK Temporal integration.

These models cross the activity boundary — they're constructed on the
workflow side and deserialized on the activity side (or vice versa).
"""

from __future__ import annotations

from typing import Any

from google.genai import types
from pydantic import BaseModel

__all__ = [
    "_GeminiApiRequest",
    "_GeminiApiResponse",
    "_GeminiApiStreamedResponse",
    "_GeminiDownloadFileRequest",
    "_GeminiInteractionIdRequest",
    "_GeminiInteractionRequest",
    "_GeminiInteractionStreamedResponse",
    "_GeminiRegisterFilesRequest",
    "_GeminiUploadFileRequest",
    "_GeminiUploadToFileSearchStoreRequest",
    "_McpCallToolRequest",
    "_SerializableHttpOptions",
]


class _SerializableHttpOptions(BaseModel):
    """Per-request HTTP options that can be serialized across the activity boundary.

    Non-serializable fields (httpx_client, httpx_async_client, aiohttp_client,
    client_args, async_client_args) must be configured at GoogleGenAIPlugin init.

    ``timeout`` is excluded because Temporal owns timeouts/retries — configure
    via ``ActivityConfig`` instead.
    """

    base_url: str | None = None
    base_url_resource_scope: str | None = None
    api_version: str | None = None
    headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None


# ── async_request models ──────────────────────────────────────────────────


class _GeminiApiRequest(BaseModel):
    """Serializable activity input for a Gemini SDK API call.

    ``streaming_topic`` / ``streaming_batch_interval_ms`` are only read by the
    streamed activity: when a topic is set, each streamed chunk is published to
    that workflow-stream topic as it arrives (in addition to being returned
    batched), so external consumers can observe the model output in real time.
    """

    http_method: str
    path: str
    request_dict: dict[str, object]
    http_options_overrides: _SerializableHttpOptions | None = None
    streaming_topic: str | None = None
    streaming_batch_interval_ms: int = 100


class _GeminiApiResponse(BaseModel):
    """Serializable activity output for a Gemini SDK API call."""

    headers: dict[str, str]
    body: str


class _GeminiApiStreamedResponse(BaseModel):
    """Serializable activity output for a batched streamed API call.

    The activity collects all streamed chunks and returns them as a list.
    The ``TemporalApiClient`` then yields them one at a time to the SDK.
    """

    chunks: list[_GeminiApiResponse]


# ── files upload/download models ──────────────────────────────────────────


class _GeminiUploadFileRequest(BaseModel):
    """Serializable activity input for a file upload.

    For file path uploads the path is resolved on the worker.  For
    in-memory uploads the raw bytes are sent across the activity boundary.
    """

    file_bytes: bytes | None = None
    file_path: str | None = None
    config: types.UploadFileConfig | None = None


class _GeminiDownloadFileRequest(BaseModel):
    """Serializable activity input for a file download."""

    file: str
    config: types.DownloadFileConfig | None = None


class _GeminiRegisterFilesRequest(BaseModel):
    """Serializable activity input for registering GCS files."""

    uris: list[str]
    config: types.RegisterFilesConfig | None = None


class _GeminiUploadToFileSearchStoreRequest(BaseModel):
    """Serializable activity input for uploading a file to a file search store."""

    file_search_store_name: str
    file_bytes: bytes | None = None
    file_path: str | None = None
    config: types.UploadToFileSearchStoreConfig | None = None


# ── interactions / agents models ──────────────────────────────────────────


class _GeminiInteractionRequest(BaseModel):
    """Serializable activity input for interactions/agents calls without an id.

    ``params`` is the caller's kwargs forwarded verbatim to the real SDK
    method on the worker — ``stream`` and ``timeout`` are popped by the
    workflow-side shim before dispatch (``stream`` selects the activity,
    ``timeout`` maps to the activity's ``start_to_close_timeout``).
    """

    params: dict[str, Any] = {}


class _GeminiInteractionIdRequest(BaseModel):
    """Serializable activity input for id-addressed interactions/agents calls."""

    id: str
    params: dict[str, Any] = {}


class _GeminiInteractionStreamedResponse(BaseModel):
    """Serializable activity output for a batched streamed interaction call.

    ``events`` is the verbatim sequence of ``InteractionSSEEvent`` objects
    yielded by the SDK's stream, each serialized via
    ``model_dump(exclude_none=True, mode="json")``.  The workflow-side shim
    rehydrates each entry with ``_temporal_interactions._deserialize`` so
    workflow code iterates the same typed events it would get from the SDK
    directly.
    """

    events: list[dict[str, Any]] = []


# ── MCP models ─────────────────────────────────────────────────────────────


class _McpCallToolRequest(BaseModel):
    """Serializable activity input for an MCP ``call_tool`` invocation.

    Carries the tool name and arguments the Gemini SDK's AFC loop selected;
    the worker-side activity forwards them to the real ``mcp.ClientSession``.
    The ``mcp.types.ListToolsResult`` / ``CallToolResult`` returned by the
    activities are themselves Pydantic models, so they serialize directly via
    the plugin's ``PydanticPayloadConverter`` and need no wrapper here.
    """

    name: str
    arguments: dict[str, Any] = {}
