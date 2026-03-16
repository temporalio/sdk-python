from __future__ import annotations

import dataclasses
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from temporalio import workflow
from temporalio.contrib.google_adk_agents._mcp import TemporalMcpToolSetProvider
from temporalio.contrib.google_adk_agents._model import invoke_model
from temporalio.contrib.pydantic import (
    PydanticPayloadConverter as _DefaultPydanticPayloadConverter,
)
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import (
    WorkflowRunner,
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


def setup_deterministic_runtime():
    """Configures ADK runtime for Temporal determinism.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    This should be called at the start of a Temporal Workflow before any ADK components
    (like SessionService) are used, if they rely on runtime.get_time() or runtime.new_uuid().
    """
    try:
        import google.adk.platform.time
        import google.adk.platform.uuid

        # Define safer, context-aware providers
        def _deterministic_time_provider() -> float:
            if workflow.in_workflow():
                return workflow.now().timestamp()
            return time.time()

        def _deterministic_id_provider() -> str:
            if workflow.in_workflow():
                return str(workflow.uuid4())
            return str(uuid.uuid4())

        google.adk.platform.time.set_time_provider(_deterministic_time_provider)
        google.adk.platform.uuid.set_id_provider(_deterministic_id_provider)
    except ImportError:
        pass
    except Exception as e:
        print(f"Warning: Failed to set deterministic runtime providers: {e}")


class GoogleAdkPlugin(SimplePlugin):
    """A Temporal Worker Plugin configured for ADK.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin configures:
    - Pydantic Payload Converter (required for ADK objects).
    - Sandbox Passthrough for google.adk and google.genai modules.
    """

    def __init__(
        self,
        toolset_providers: list[TemporalMcpToolSetProvider] | None = None,
    ):
        """Initializes the Temporal ADK Plugin.

        Args:
            toolset_providers: Optional list of toolset providers for MCP integration.
        """

        @asynccontextmanager
        async def run_context() -> AsyncIterator[None]:
            setup_deterministic_runtime()
            yield

        def workflow_runner(runner: WorkflowRunner | None) -> WorkflowRunner:
            if not runner:
                raise ValueError("No WorkflowRunner provided to the ADK plugin.")

            # If in sandbox, add additional passthrough
            if isinstance(runner, SandboxedWorkflowRunner):
                return dataclasses.replace(
                    runner,
                    restrictions=runner.restrictions.with_passthrough_modules(
                        "google.adk", "google.genai", "mcp"
                    ),
                )
            return runner

        new_activities = [invoke_model]
        if toolset_providers is not None:
            for toolset_provider in toolset_providers:
                new_activities.extend(toolset_provider._get_activities())

        super().__init__(
            name="google.AdkPlugin",
            data_converter=self._configure_data_converter,
            activities=new_activities,
            run_context=lambda: run_context(),
            workflow_runner=workflow_runner,
        )

    def _configure_data_converter(
        self, converter: DataConverter | None
    ) -> DataConverter:
        if converter is None:
            return DataConverter(
                payload_converter_class=_DefaultPydanticPayloadConverter
            )
        elif converter.payload_converter_class is DefaultPayloadConverter:
            return dataclasses.replace(
                converter, payload_converter_class=_DefaultPydanticPayloadConverter
            )
        return converter
