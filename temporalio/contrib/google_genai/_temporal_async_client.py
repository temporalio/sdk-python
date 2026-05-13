"""Temporal-aware AsyncClient shim.

``TemporalAsyncClient`` is an ``AsyncClient`` subclass that wires up
Temporal-aware replacements for modules that need special handling
(files, file search stores).
"""

from __future__ import annotations

from google.genai.client import AsyncClient

from temporalio.contrib.google_genai._temporal_api_client import (
    TemporalApiClient,
)
from temporalio.contrib.google_genai._temporal_file_search_stores import (
    TemporalAsyncFileSearchStores,
)
from temporalio.contrib.google_genai._temporal_files import (
    TemporalAsyncFiles,
)
from temporalio.workflow import ActivityConfig


class TemporalAsyncClient(AsyncClient):
    """``AsyncClient`` subclass that uses Temporal-aware modules.

    Replaces ``AsyncFiles`` with ``TemporalAsyncFiles`` and
    ``AsyncFileSearchStores`` with ``TemporalAsyncFileSearchStores``
    so that file upload/download operations and file search store uploads
    run entirely inside Temporal activities.

    Other modules (models, tunings, caches, batches, live, tokens,
    operations) are inherited unchanged and work through
    ``TemporalApiClient``'s activity-backed HTTP methods.
    """

    def __init__(
        self,
        api_client: TemporalApiClient,
        activity_config: ActivityConfig | None = None,
    ) -> None:
        """Initialize with Temporal-aware files and file search stores."""
        super().__init__(api_client)
        self._files = TemporalAsyncFiles(api_client, activity_config)
        self._file_search_stores = TemporalAsyncFileSearchStores(
            api_client, activity_config
        )
