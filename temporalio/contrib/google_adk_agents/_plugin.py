from __future__ import annotations

import dataclasses
import random
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from temporalio import workflow
from temporalio.contrib.google_adk_agents._mcp import (
    TemporalMcpToolSetProvider,
    TemporalStatefulMcpToolSetProvider,
)
from temporalio.contrib.google_adk_agents._model import (
    invoke_model,
    invoke_model_streaming,
)
from temporalio.contrib.pydantic import (
    PydanticPayloadConverter,
    ToJsonOptions,
)
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import (
    WorkflowRunner,
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


def _install_provider(module: Any, var_name: str, provider: Callable[[], Any]) -> None:
    """Installs a provider as the process-wide default for an ADK platform seam.

    ADK's platform providers are held in ``ContextVar``s. Setting them with the
    public ``set_*_provider`` helpers only affects the calling context, and
    Temporal executes workflow code on executor threads whose contexts never
    see that call — so the provider must be installed at the ContextVar
    *default* level to be visible inside workflows. Rebinding the module's
    ContextVar with a new default preserves the public setters' semantics
    (a context-local ``set_*_provider`` still overrides the default).
    """
    from contextvars import ContextVar

    context_var = getattr(module, var_name)
    setattr(module, var_name, ContextVar(context_var.name, default=provider))


def setup_deterministic_runtime():
    """Configures ADK runtime for Temporal determinism.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    Installs Temporal-aware time, uuid, and (when the seam exists) random
    providers as the process-wide defaults for ADK's ``google.adk.platform``
    seams. Inside a workflow they derive from ``workflow.now()`` /
    ``workflow.uuid4()`` / ``workflow.random()`` so replays are
    deterministic; outside a workflow they fall back to the real primitives.
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

        _install_provider(
            google.adk.platform.time,
            "_time_provider_context_var",
            _deterministic_time_provider,
        )
        _install_provider(
            google.adk.platform.uuid,
            "_id_provider_context_var",
            _deterministic_id_provider,
        )
    except ImportError:
        pass
    except Exception as e:
        print(f"Warning: Failed to set deterministic runtime providers: {e}")

    try:
        # Available on ADK versions that route retry jitter through the
        # platform random seam; a no-op ImportError on older versions.
        import google.adk.platform._random  # type: ignore

        _local_random = random.Random()

        def _deterministic_random_provider() -> random.Random:
            if workflow.in_workflow():
                return workflow.random()
            return _local_random

        _install_provider(
            google.adk.platform._random,
            "_random_provider_context_var",
            _deterministic_random_provider,
        )
    except ImportError:
        pass
    except Exception as e:
        print(f"Warning: Failed to set deterministic random provider: {e}")


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
        toolset_providers: list[
            TemporalMcpToolSetProvider | TemporalStatefulMcpToolSetProvider
        ]
        | None = None,
    ):
        """Initializes the Temporal ADK Plugin.

        Args:
            toolset_providers: Optional list of stateless
                (:class:`TemporalMcpToolSetProvider`) or stateful
                (:class:`TemporalStatefulMcpToolSetProvider`) toolset providers
                for MCP integration.
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

        # Annotate as Sequence[Callable[..., Any]] because invoke_model
        # and invoke_model_streaming have different signatures, so the
        # inferred list type would not satisfy SimplePlugin's parameter.
        new_activities: list[Callable[..., Any]] = [
            invoke_model,
            invoke_model_streaming,
        ]
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
            return DataConverter(payload_converter_class=_AdkPayloadConverter)
        elif converter.payload_converter_class is DefaultPayloadConverter:
            return dataclasses.replace(
                converter, payload_converter_class=_AdkPayloadConverter
            )
        return converter


class _AdkPayloadConverter(PydanticPayloadConverter):
    """PayloadConverter for Google ADK that strips unset None fields."""

    def __init__(self) -> None:
        super().__init__(ToJsonOptions(exclude_unset=True))
