"""Temporal plugin for Google Gemini SDK integration."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from typing import TYPE_CHECKING

import google.auth.credentials
from google.genai import Client as GeminiClient

from temporalio.contrib.google_genai._errors import GoogleGenAIError
from temporalio.contrib.google_genai._gemini_activity import GeminiApiCaller
from temporalio.contrib.pydantic import PydanticPayloadConverter
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

if TYPE_CHECKING:
    from temporalio.contrib.google_genai._mcp import McpSessionFactory


_RETRY_OPTIONS_MESSAGE = (
    "genai.Client is configured with http_options.retry_options, but Temporal "
    "owns retries for durable execution. Remove retry_options from the client "
    "and configure retries with the activity retry_policy instead — e.g. "
    "TemporalAsyncClient(activity_config=ActivityConfig(retry_policy=...)) or "
    "activity_as_tool(fn, activity_config=ActivityConfig(retry_policy=...))."
)


def _reject_sdk_retries(client: GeminiClient) -> None:
    """Raise if the client enables the SDK's own retry loop.

    Temporal must own retries so each attempt is a separate, observable activity
    attempt; an SDK-internal retry loop would hide retries inside one activity
    and compound with Temporal's retry policy.
    """
    http_options = getattr(client._api_client, "_http_options", None)
    if http_options is not None and getattr(http_options, "retry_options", None):
        raise ValueError(_RETRY_OPTIONS_MESSAGE)


def _data_converter(converter: DataConverter | None) -> DataConverter:
    if converter is None:
        return DataConverter(payload_converter_class=PydanticPayloadConverter)
    elif converter.payload_converter_class is DefaultPayloadConverter:
        return dataclasses.replace(
            converter, payload_converter_class=PydanticPayloadConverter
        )
    return converter


class GoogleGenAIPlugin(SimplePlugin):
    """A Temporal Worker Plugin configured for the Google Gemini SDK.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin registers the ``gemini_api_client_async_request`` activity
    using the provided ``genai.Client`` with real credentials.  Workflows
    construct a :class:`temporalio.contrib.google_genai.TemporalAsyncClient`
    to get an ``AsyncClient`` backed by a ``TemporalApiClient`` that routes all
    API calls through this activity.

    No credentials are passed to or from the workflow.  Auth material never
    appears in Temporal's event history.

    Example (Gemini Developer API)::

        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        plugin = GoogleGenAIPlugin(client)

    Example (Vertex AI)::

        client = genai.Client(
            vertexai=True, project="my-project", location="us-central1",
        )
        plugin = GoogleGenAIPlugin(client)

    Example (with separate GCS credentials for file registration)::

        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        gcs_creds, _ = google.auth.default()
        plugin = GoogleGenAIPlugin(client, extra_credentials=gcs_creds)
    """

    def __init__(
        self,
        client: GeminiClient,
        extra_credentials: google.auth.credentials.Credentials | None = None,
        mcp_servers: dict[str, McpSessionFactory] | None = None,
        mcp_connection_idle_timeout: timedelta | None = None,
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
            mcp_servers: MCP servers to expose to workflows, keyed by name.
                Each value is a factory returning an async context manager that
                yields a connected, initialized ``mcp.ClientSession``.  A
                workflow references a server by name with
                ``TemporalMcpClientSession(name)`` in a ``generate_content``
                ``tools`` list; ``list_tools`` / ``call_tool`` then run as the
                ``{name}-list-tools`` / ``{name}-call-tool`` activities against a
                worker-side connection.  Requires the ``mcp`` package.
            mcp_connection_idle_timeout: How long a worker-process MCP
                connection stays open while idle before being disconnected
                (the timer resets on each reuse).  Defaults to 5 minutes.
        """
        _reject_sdk_retries(client)
        self._api_caller = GeminiApiCaller(client, credentials=extra_credentials)

        activities = list(self._api_caller.activities())
        if mcp_servers:
            # Imported lazily: ``mcp`` is an optional dependency, only needed
            # when MCP servers are registered.
            from temporalio.contrib.google_genai._mcp import build_mcp_activities

            activities.extend(
                build_mcp_activities(mcp_servers, mcp_connection_idle_timeout)
            )

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if not runner:
                raise ValueError("No WorkflowRunner provided to GoogleGenAIPlugin.")
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        # The SDK's request formatting + AFC loop run in-workflow
                        # and validate google.genai's Pydantic models; mcp is
                        # imported to subclass ClientSession.  pydantic itself is
                        # in the SDK default passthrough, but its compiled core
                        # and Annotated helper are not, so extend them.
                        "google.genai",
                        "mcp",
                        "pydantic_core",
                        "annotated_types",
                    ),
                )
            return runner

        super().__init__(
            name="google_genai.GoogleGenAIPlugin",
            data_converter=_data_converter,
            activities=activities,
            workflow_runner=workflow_runner,
            workflow_failure_exception_types=[GoogleGenAIError],
        )
