from __future__ import annotations

import dataclasses
import sys
import time
import uuid
import warnings
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import FrameType
from typing import Any

import opentelemetry._logs
import opentelemetry.metrics
import opentelemetry.trace
from opentelemetry._logs import LoggerProvider, NoOpLoggerProvider
from opentelemetry.metrics import MeterProvider, NoOpMeterProvider
from opentelemetry.sdk._logs import LoggerProvider as SdkLoggerProvider
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.trace import (
    NoOpTracerProvider,
    ProxyTracerProvider,
    TracerProvider,
)

from temporalio import workflow
from temporalio.contrib.google_adk_agents._mcp import TemporalMcpToolSetProvider
from temporalio.contrib.google_adk_agents._model import (
    invoke_model,
    invoke_model_streaming,
)
from temporalio.contrib.opentelemetry import (
    ReplaySafeLoggerProvider,
    ReplaySafeMeterProvider,
    ReplaySafeTracerProvider,
)
from temporalio.contrib.pydantic import (
    PydanticPayloadConverter,
    ToJsonOptions,
)
from temporalio.converter import DataConverter, DefaultPayloadConverter
from temporalio.plugin import SimplePlugin
from temporalio.worker import (
    ReplayerConfig,
    WorkerConfig,
    WorkflowRunner,
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

# Each classifier below returns True when the provider is replay-safe
# (including providers that drop all recordings), False when positively
# identified as replay-unsafe (an OpenTelemetry SDK provider), and None when
# it cannot be classified. Unknown provider types (e.g. a custom provider
# delegating to a replay-safe one) must not warn: a false positive is worse
# than a missed warning.


def _meter_provider_replay_safe(provider: MeterProvider) -> bool | None:
    if isinstance(provider, (ReplaySafeMeterProvider, NoOpMeterProvider)):
        return True
    try:
        # Unlike tracing's public ProxyTracerProvider, the proxy (unset) meter
        # provider has no public counterpart. Import the private class lazily
        # so a moved or removed symbol cannot break module import; it is
        # present in opentelemetry-api 1.12 through at least 1.42.
        from opentelemetry.metrics._internal import _ProxyMeterProvider

        if isinstance(provider, _ProxyMeterProvider):
            return True
    except ImportError:
        pass
    return False if isinstance(provider, SdkMeterProvider) else None


def _tracer_provider_replay_safe(provider: TracerProvider) -> bool | None:
    if isinstance(
        provider,
        (ReplaySafeTracerProvider, NoOpTracerProvider, ProxyTracerProvider),
    ):
        return True
    return False if isinstance(provider, SdkTracerProvider) else None


def _logger_provider_replay_safe(provider: LoggerProvider) -> bool | None:
    if isinstance(provider, (ReplaySafeLoggerProvider, NoOpLoggerProvider)):
        return True
    try:
        # The proxy (unset) logger provider has no public counterpart either;
        # present in opentelemetry-api 1.23 through at least 1.42.
        from opentelemetry._logs._internal import ProxyLoggerProvider

        if isinstance(provider, ProxyLoggerProvider):
            return True
    except ImportError:
        pass
    return False if isinstance(provider, SdkLoggerProvider) else None


def _stacklevel_outside_temporalio() -> int:
    # Attribute provider warnings to the nearest frame outside temporalio,
    # e.g. the user's Worker(...)/Replayer(...) call or a user plugin that
    # delegates here, however many plugin frames sit in between.
    level = 1
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module != "temporalio" and not module.startswith("temporalio."):
            return level
        frame = frame.f_back
        level += 1
    return 1


def _warn_if_global_otel_providers_not_replay_safe() -> None:
    # ADK records metrics, spans, and log events through the process-global
    # OpenTelemetry providers from code that runs workflow-side, so a
    # non-replay-safe global provider re-emits that telemetry on every
    # workflow replay. Unset (proxy) and no-op providers drop recordings and
    # are fine.
    stacklevel = _stacklevel_outside_temporalio()
    if _meter_provider_replay_safe(opentelemetry.metrics.get_meter_provider()) is False:
        warnings.warn(
            "The global OpenTelemetry MeterProvider is not replay-safe: Google ADK "
            "records metrics from workflow code, so every workflow replay will "
            "re-record them. Wrap your provider in "
            "temporalio.contrib.opentelemetry.ReplaySafeMeterProvider and make it "
            "the first and only global provider set: "
            "opentelemetry.metrics.set_meter_provider(ReplaySafeMeterProvider(provider))",
            UserWarning,
            stacklevel=stacklevel,
        )
    if _tracer_provider_replay_safe(opentelemetry.trace.get_tracer_provider()) is False:
        warnings.warn(
            "The global OpenTelemetry TracerProvider is not replay-safe: Google ADK "
            "creates spans from workflow code, so every workflow replay will "
            "re-emit them. Install a replay-safe provider: "
            "opentelemetry.trace.set_tracer_provider("
            "temporalio.contrib.opentelemetry.create_tracer_provider())",
            UserWarning,
            stacklevel=stacklevel,
        )
    if _logger_provider_replay_safe(opentelemetry._logs.get_logger_provider()) is False:
        warnings.warn(
            "The global OpenTelemetry LoggerProvider is not replay-safe: Google ADK "
            "emits log events (e.g. gen_ai.choice) from workflow code, so every "
            "workflow replay will re-emit them. Wrap your provider in "
            "temporalio.contrib.opentelemetry.ReplaySafeLoggerProvider and make it "
            "the first and only global provider set: "
            "opentelemetry._logs.set_logger_provider(ReplaySafeLoggerProvider(provider))",
            UserWarning,
            stacklevel=stacklevel,
        )


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

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        """See base class. Also warns when the global OpenTelemetry providers
        are not replay-safe, since ADK telemetry would duplicate on replay.
        """
        _warn_if_global_otel_providers_not_replay_safe()
        return super().configure_worker(config)

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        """See base class. Also warns when the global OpenTelemetry providers
        are not replay-safe, since every replayed workflow would re-emit ADK
        telemetry.
        """
        _warn_if_global_otel_providers_not_replay_safe()
        return super().configure_replayer(config)

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
