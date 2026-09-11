from __future__ import annotations

import contextvars
import dataclasses
import inspect
import random
import threading
import time
import uuid
import warnings
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import FrameType
from typing import Any

import opentelemetry.metrics
import opentelemetry.trace

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
    ReplayerConfig,
    WorkerConfig,
    WorkflowRunner,
)
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner


def _stacklevel_outside_temporalio() -> int:
    # Attribute provider warnings to the nearest frame outside temporalio,
    # e.g. the user's Worker(...)/Replayer(...) call or a user plugin that
    # delegates here, however many plugin frames sit in between.
    level = 1
    own_frame: FrameType | None = inspect.currentframe()
    frame = own_frame.f_back if own_frame is not None else None
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
    # workflow replay. Warn only on providers positively identified as
    # replay-unsafe: an OpenTelemetry SDK provider used directly as the
    # global. Anything else stays silent -- unset (proxy) and no-op providers
    # drop recordings, and unknown provider types (e.g. a custom provider
    # delegating to a replay-safe one) cannot be classified, where a false
    # positive is worse than a missed warning. The SDK logger provider is not
    # checked because its class is only importable from the underscore
    # namespace opentelemetry.sdk._logs while opentelemetry-python has not
    # promoted the logs SDK to a public namespace.
    try:
        from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
        from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
    except ImportError:
        # Without the opentelemetry-sdk package installed no SDK provider can
        # exist, so there is nothing replay-unsafe to warn about.
        return
    stacklevel = _stacklevel_outside_temporalio()
    if isinstance(opentelemetry.metrics.get_meter_provider(), SdkMeterProvider):
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
    if isinstance(opentelemetry.trace.get_tracer_provider(), SdkTracerProvider):
        warnings.warn(
            "The global OpenTelemetry TracerProvider is not replay-safe: Google ADK "
            "creates spans from workflow code, so every workflow replay will "
            "re-emit them. Install a replay-safe provider: "
            "opentelemetry.trace.set_tracer_provider("
            "temporalio.contrib.opentelemetry.create_tracer_provider())",
            UserWarning,
            stacklevel=stacklevel,
        )


def _deterministic_time_provider() -> float:
    if workflow.in_workflow():
        return workflow.time()
    return time.time()


def _deterministic_id_provider() -> str:
    if workflow.in_workflow():
        return str(workflow.uuid4())
    return str(uuid.uuid4())


# ADK's own default is one process-wide random.Random, and its
# set_random_provider docstring asks providers to return an existing instance
# so RNG state carries across get_random() calls; keep one for outside
# workflows too.
_random_outside_workflow = random.Random()


def _deterministic_random_provider() -> random.Random:
    if workflow.in_workflow():
        return workflow.random()
    return _random_outside_workflow


_install_provider_lock = threading.Lock()


def _install_provider(module: Any, var_name: str, provider: Callable[[], Any]) -> None:
    """Rebinds an ADK platform ContextVar to one whose default is ``provider``.

    ADK's ``set_*_provider`` functions set a value in the calling context only.
    Workflow tasks run on worker threads, which start with an empty
    contextvars context, so a value set from the worker's event loop never
    reaches them and ADK falls back to its wall-clock and random defaults
    there. A ContextVar's default, unlike a set value, is visible from every
    context, so the module's variable is replaced with one that defaults to
    ``provider``. ADK's ``set_*_provider`` and ``reset_*_provider`` operate on
    the new variable from then on; a value set on the old one beforehand is
    orphaned, so it is warned about. A no-op when ``provider`` is already the
    default.
    """
    current: contextvars.ContextVar[Callable[[], Any]] = getattr(module, var_name)
    try:
        default = contextvars.Context().run(current.get)
    except LookupError:
        default = None
    if default is provider:
        return
    if current.get(default) is not default:
        warnings.warn(
            f"Replacing the {module.__name__} provider set in this context before "
            "GoogleAdkPlugin installed its deterministic providers; it will not "
            "take effect. Set ADK provider overrides after the worker starts or "
            "from workflow code.",
            UserWarning,
            stacklevel=_stacklevel_outside_temporalio(),
        )
    setattr(module, var_name, contextvars.ContextVar(current.name, default=provider))


def setup_deterministic_runtime() -> None:
    """Installs Temporal's deterministic time, id, and random providers for ADK.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    The providers become the process-wide defaults of ADK's
    ``google.adk.platform`` time, uuid, and random seams, so they apply inside
    workflow tasks (which run on worker threads with an empty contextvars
    context) as well as in the calling context. Inside a workflow they return
    ``workflow.time()``, ``workflow.uuid4()``, and ``workflow.random()``, so
    ADK-generated ids and retry jitter are reproducible on replay; like those
    functions, id and random generation raise
    :class:`temporalio.workflow.ReadOnlyContextError` in query handlers and
    update validators. Outside a workflow in the same process (activities,
    client code) they fall back to ``time.time()``, ``uuid.uuid4()``, and a
    process-wide ``random.Random``.

    Overrides through ADK's ``set_*_provider`` functions must be made after
    this runs (after the worker starts, or from workflow code); one made
    earlier is replaced, with a warning.

    :class:`GoogleAdkPlugin` calls this when a worker or replayer starts.
    Calling it again is a no-op.
    """
    import google.adk.platform._random
    import google.adk.platform.time
    import google.adk.platform.uuid

    with _install_provider_lock:
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
        _install_provider(
            google.adk.platform._random,
            "_random_provider_context_var",
            _deterministic_random_provider,
        )


class GoogleAdkPlugin(SimplePlugin):
    """A Temporal Worker Plugin configured for ADK.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin configures:
    - Pydantic Payload Converter (required for ADK objects).
    - Sandbox Passthrough for google.adk and google.genai modules.
    - ADK's time, id, and random providers, so ADK-generated ids and retry
      jitter come from the workflow's deterministic clock and random stream
      (see :func:`setup_deterministic_runtime`).

    At worker and replayer configuration time it also warns when the global
    OpenTelemetry meter or tracer provider is not replay-safe, since ADK
    telemetry recorded from workflow code would duplicate on replay.
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

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        """See base class. Also warns when the global OpenTelemetry meter or
        tracer provider is not replay-safe, since ADK telemetry would
        duplicate on replay.
        """
        _warn_if_global_otel_providers_not_replay_safe()
        return super().configure_worker(config)

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        """See base class. Also warns when the global OpenTelemetry meter or
        tracer provider is not replay-safe, since every replayed workflow
        would re-emit ADK telemetry.
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
