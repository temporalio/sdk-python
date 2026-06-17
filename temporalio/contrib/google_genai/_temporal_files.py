"""Temporal-aware AsyncFiles shim.

``TemporalAsyncFiles`` is an ``AsyncFiles`` subclass whose ``upload``
and ``download`` methods dispatch through Temporal activities so the
entire file operation (including filesystem access) runs on the
activity worker.
"""

from __future__ import annotations

import io
import os
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import google.auth.credentials
from google.genai import types
from google.genai.files import AsyncFiles

from temporalio import workflow as temporal_workflow
from temporalio.contrib.google_genai._models import (
    _GeminiDownloadFileRequest,
    _GeminiRegisterFilesRequest,
    _GeminiUploadFileRequest,
)
from temporalio.contrib.google_genai._temporal_api_client import (
    _TemporalApiClient,
    _validate_http_options,
)
from temporalio.workflow import ActivityConfig


class TemporalAsyncFiles(AsyncFiles):
    """``AsyncFiles`` subclass that routes ``upload`` and ``download`` through activities.

    The entire file operation — including filesystem access, resumable
    upload negotiation, and chunked transfer — runs inside a Temporal
    activity on the worker.  ``get``, ``delete``, and ``list`` are
    inherited from ``AsyncFiles`` and already work through the
    ``_TemporalApiClient``'s ``async_request`` activity.
    """

    def __init__(
        self,
        api_client: _TemporalApiClient,
        activity_config: ActivityConfig | None = None,
    ) -> None:
        """Initialize with activity config for file operation timeouts."""
        super().__init__(api_client)
        self._activity_config = (
            ActivityConfig(start_to_close_timeout=timedelta(seconds=60))
            if activity_config is None
            else activity_config
        )

    async def upload(
        self,
        *,
        file: str | os.PathLike[str] | io.IOBase,
        config: types.UploadFileConfigOrDict | None = None,
    ) -> types.File:
        """Upload a file via a Temporal activity.

        Accepts a file path (resolved on the worker), ``os.PathLike``, or
        an ``io.IOBase`` (bytes sent across the activity boundary).
        """
        act_config: ActivityConfig = {**self._activity_config}
        if "summary" not in act_config:
            act_config["summary"] = "files.upload"

        upload_config = None
        if config is not None:
            if isinstance(config, dict):
                upload_config = types.UploadFileConfig.model_validate(config)
            else:
                upload_config = config
            _validate_http_options(upload_config.http_options)

        if isinstance(file, io.IOBase):
            file_bytes = file.read()
            if not isinstance(file_bytes, bytes):
                raise TypeError(
                    "file must be a binary stream when passing an io.IOBase; "
                    f"file.read() must return bytes (got {type(file_bytes).__name__})"
                )
            req = _GeminiUploadFileRequest(file_bytes=file_bytes, config=upload_config)
        elif isinstance(file, str):
            req = _GeminiUploadFileRequest(file_path=file, config=upload_config)
        else:
            # os.PathLike — convert via __fspath__() to avoid importing os
            req = _GeminiUploadFileRequest(
                file_path=file.__fspath__(), config=upload_config
            )

        return await temporal_workflow.execute_activity(
            "gemini_files_upload",
            req,
            result_type=types.File,
            **act_config,
        )

    async def download(
        self,
        *,
        file: str | types.File,
        config: types.DownloadFileConfigOrDict | None = None,
    ) -> bytes:
        """Download a file via a Temporal activity."""
        act_config: ActivityConfig = {**self._activity_config}
        if "summary" not in act_config:
            act_config["summary"] = "files.download"

        download_config = None
        if config is not None:
            if isinstance(config, dict):
                download_config = types.DownloadFileConfig.model_validate(config)
            else:
                download_config = config
            _validate_http_options(download_config.http_options)

        if isinstance(file, types.File):
            if not file.name:
                raise ValueError("File object must have a name to download.")
            file_name = file.name
        else:
            file_name = file

        return await temporal_workflow.execute_activity(
            "gemini_files_download",
            _GeminiDownloadFileRequest(file=file_name, config=download_config),
            result_type=bytes,
            **act_config,
        )

    async def register_files(
        self,
        *,
        auth: google.auth.credentials.Credentials,
        uris: list[str],
        config: types.RegisterFilesConfigOrDict | None = None,
    ) -> types.RegisterFilesResponse:
        """Register GCS files via a Temporal activity.

        .. note::
            The ``auth`` parameter is **ignored**.  The activity uses
            ``credentials`` if provided to ``GoogleGenAIPlugin``,
            otherwise falls back to the ``genai.Client``'s own credentials.
            Either way, those credentials must have access to the GCS URIs
            being registered.
        """
        act_config: ActivityConfig = {**self._activity_config}
        if "summary" not in act_config:
            act_config["summary"] = "files.register_files"

        register_config = None
        if config is not None:
            if isinstance(config, dict):
                register_config = types.RegisterFilesConfig.model_validate(config)
            else:
                register_config = config
            _validate_http_options(register_config.http_options)

        return await temporal_workflow.execute_activity(
            "gemini_files_register",
            _GeminiRegisterFilesRequest(uris=uris, config=register_config),
            result_type=types.RegisterFilesResponse,
            **act_config,
        )
