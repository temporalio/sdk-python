"""Temporal-aware AsyncFileSearchStores shim.

``TemporalAsyncFileSearchStores`` is an ``AsyncFileSearchStores`` subclass
whose ``upload_to_file_search_store`` method dispatches through a Temporal
activity so the entire upload (including filesystem access and resumable
upload negotiation) runs on the activity worker.
"""

from __future__ import annotations

import io
import os
from datetime import timedelta

from google.genai import types
from google.genai.file_search_stores import AsyncFileSearchStores

from temporalio import workflow as temporal_workflow
from temporalio.contrib.google_genai._models import (
    _GeminiUploadToFileSearchStoreRequest,
)
from temporalio.contrib.google_genai._temporal_api_client import (
    _TemporalApiClient,
    _validate_http_options,
)
from temporalio.workflow import ActivityConfig


class TemporalAsyncFileSearchStores(AsyncFileSearchStores):
    """``AsyncFileSearchStores`` subclass that routes ``upload_to_file_search_store`` through an activity.

    The entire upload operation — including filesystem access, resumable
    upload negotiation, and chunked transfer — runs inside a Temporal
    activity on the worker.  All other methods (``create``, ``get``,
    ``delete``, ``list``, ``import_file``, ``documents``) are inherited
    and already work through the ``_TemporalApiClient``'s ``async_request``
    activity.
    """

    def __init__(
        self,
        api_client: _TemporalApiClient,
        activity_config: ActivityConfig | None = None,
    ) -> None:
        """Initialize with activity config for upload timeouts."""
        super().__init__(api_client)
        self._activity_config = (
            ActivityConfig(start_to_close_timeout=timedelta(seconds=60))
            if activity_config is None
            else activity_config
        )

    async def upload_to_file_search_store(
        self,
        *,
        file_search_store_name: str,
        file: str | os.PathLike[str] | io.IOBase,
        config: types.UploadToFileSearchStoreConfigOrDict | None = None,
    ) -> types.UploadToFileSearchStoreOperation:
        """Upload a file to a file search store via a Temporal activity.

        Accepts a file path (resolved on the worker), ``os.PathLike``, or
        an ``io.IOBase`` (bytes sent across the activity boundary).
        """
        act_config: ActivityConfig = {**self._activity_config}
        if "summary" not in act_config:
            act_config["summary"] = "file_search_stores.upload"

        upload_config = None
        if config is not None:
            if isinstance(config, dict):
                upload_config = types.UploadToFileSearchStoreConfig.model_validate(
                    config
                )
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
            req = _GeminiUploadToFileSearchStoreRequest(
                file_search_store_name=file_search_store_name,
                file_bytes=file_bytes,
                config=upload_config,
            )
        elif isinstance(file, str):
            req = _GeminiUploadToFileSearchStoreRequest(
                file_search_store_name=file_search_store_name,
                file_path=file,
                config=upload_config,
            )
        else:
            req = _GeminiUploadToFileSearchStoreRequest(
                file_search_store_name=file_search_store_name,
                file_path=file.__fspath__(),
                config=upload_config,
            )

        return await temporal_workflow.execute_activity(
            "gemini_file_search_stores_upload",
            req,
            result_type=types.UploadToFileSearchStoreOperation,
            **act_config,
        )
