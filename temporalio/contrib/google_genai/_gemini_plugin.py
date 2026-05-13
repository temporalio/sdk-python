"""Temporal plugin for Google Gemini SDK integration."""

from __future__ import annotations

import dataclasses

import google.auth.credentials
from google.genai import Client as GeminiClient

from temporalio.contrib.google_genai._gemini_activity import GeminiApiCaller
from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


def _data_converter(converter: DataConverter | None) -> DataConverter:
    if converter is None:
        return DataConverter(payload_converter_class=PydanticPayloadConverter)
    elif converter.payload_converter_class is DefaultPayloadConverter:
        return dataclasses.replace(
            converter, payload_converter_class=PydanticPayloadConverter
        )
    return converter


class GeminiPlugin(SimplePlugin):
    """A Temporal Worker Plugin configured for the Google Gemini SDK.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin registers the ``gemini_api_client_async_request`` activity
    using the provided ``genai.Client`` with real credentials.  Workflows use
    :func:`~temporalio.contrib.google_genai.workflow.gemini_client` to
    get an ``AsyncClient`` backed by a ``TemporalApiClient`` that routes all
    API calls through this activity.

    No credentials are passed to or from the workflow.  Auth material never
    appears in Temporal's event history.

    Example (Gemini Developer API)::

        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        plugin = GeminiPlugin(client)

    Example (Vertex AI)::

        client = genai.Client(
            vertexai=True, project="my-project", location="us-central1",
        )
        plugin = GeminiPlugin(client)

    Example (with separate GCS credentials for file registration)::

        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        gcs_creds, _ = google.auth.default()
        plugin = GeminiPlugin(client, extra_credentials=gcs_creds)
    """

    def __init__(
        self,
        client: GeminiClient,
        extra_credentials: google.auth.credentials.Credentials | None = None,
    ) -> None:
        """Initialize the Gemini plugin.

        Args:
            client: A fully configured ``genai.Client`` instance.
                All credential management, HTTP client configuration, etc.
                is the responsibility of the caller.
            extra_credentials: Optional Google Cloud credentials used for
                operations that require explicit auth (e.g.
                ``files.register_files()``).  If not provided, the
                client's own credentials are used.
        """
        self._api_caller = GeminiApiCaller(client, credentials=extra_credentials)

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if not runner:
                raise ValueError("No WorkflowRunner provided to GeminiPlugin.")
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        "google.genai"
                    ),
                )
            return runner

        super().__init__(
            name="GeminiPlugin",
            data_converter=_data_converter,
            activities=self._api_caller.activities(),
            workflow_runner=workflow_runner,
        )
