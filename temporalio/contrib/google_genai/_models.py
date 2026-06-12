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
    "_GeminiRegisterFilesRequest",
    "_GeminiUploadFileRequest",
    "_GeminiUploadToFileSearchStoreRequest",
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
    """Serializable activity input for a Gemini SDK API call."""

    http_method: str
    path: str
    request_dict: dict[str, object]
    http_options_overrides: _SerializableHttpOptions | None = None


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
