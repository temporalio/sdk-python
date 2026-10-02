from __future__ import annotations

import contextvars
import dataclasses
import inspect
import random
import threading
import time
import uuid
import warnings
import weakref
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
    # workflow.time() in every in-workflow context, read-only ones included: a
    # dynamic workflow's ``dynamic_config`` runs read-only and is replayed, so
    # a wall-clock value there would not be replay-safe. In a query handler
    # the value is the current activation's timestamp rather than the wall
    # clock, which is harmless because nothing a query computes is persisted.
    if workflow.in_workflow():
        return workflow.time()
    return time.time()


# Each run's private stream, keyed by the SDK's per-run runtime object: that
# exists during the workflow's __init__ (workflow.instance() does not yet) and
# leaves the user's class alone (it may use __slots__). Entries go away with
# the run.
_adk_randoms: weakref.WeakKeyDictionary[workflow._Runtime, random.Random] = (
    weakref.WeakKeyDictionary()
)
_adk_randoms_lock = threading.Lock()


def _workflow_adk_random() -> random.Random:
    # ADK draws from a private stream (a workflow.new_random() per run) rather
    # than sharing workflow.random(), so how many values ADK consumes never
    # shifts the sequence user code sees. Read-only code must not touch that
    # stream: a draw there would advance it and diverge later activations
    # from replay. Query handlers and update validators are never replayed, so
    # they get a fresh unseeded generator instead. Every other read-only
    # context (a dynamic workflow's ``dynamic_config``, a patch activation
    # callback) is replayed, so drawing there is an error, as it is for
    # workflow.random(), rather than a value that differs on replay.
    runtime = workflow._Runtime.current()
    if runtime.workflow_is_read_only():
        if runtime.workflow_in_query_or_validator():
            return random.Random()
        raise workflow.ReadOnlyContextError(
            "While in read-only function, action attempted: ADK random"
        )
    with _adk_randoms_lock:
        rng = _adk_randoms.get(runtime)
        if rng is None:
            rng = workflow.new_random()
            _adk_randoms[runtime] = rng
    return rng


def _uuid4_from(rng: random.Random) -> uuid.UUID:
    # Same construction as workflow.uuid4(), drawn from the given stream.
    return uuid.UUID(bytes=rng.getrandbits(16 * 8).to_bytes(16, "big"), version=4)


def _deterministic_id_provider() -> str:
    if workflow.in_workflow():
        return str(_uuid4_from(_workflow_adk_random()))
    return str(uuid.uuid4())


def _deterministic_random_provider() -> random.Random:
    # Outside a workflow, a fresh unseeded generator per call. ADK's
    # set_random_provider docstring asks providers to return an existing
    # instance so a seeded generator keeps its sequence across get_random()
    # calls; an unseeded one draws fresh OS entropy either way, and ADK's only
    # caller uses the result immediately (retry jitter).
    if workflow.in_workflow():
        return _workflow_adk_random()
    return random.Random()


_install_provider_lock = threading.Lock()


def _install_provider(
    module: Any, var_name: str, default_name: str, provider: Callable[[], Any]
) -> None:
    """Makes ``provider`` an ADK platform seam's default, everywhere.

    ADK's ``set_*_provider`` functions set a value in the calling context only.
    Workflow tasks run on worker threads, which start with an empty
    contextvars context, so a value set from the worker's event loop never
    reaches them and ADK falls back to its wall-clock and random defaults
    there. A ContextVar's default, unlike a set value, is visible from every
    context, so the module's variable is replaced with one that defaults to
    ``provider``. The module's ``_default_*`` binding is rebound too, because
    ``reset_*_provider`` restores that binding: without this, an override
    followed by a reset would land on the standard-library provider rather
    than back on ``provider``. ADK's ``set_*_provider`` and
    ``reset_*_provider`` operate on the new variable from then on; a value set
    on the old one beforehand is orphaned, so it is warned about. A no-op when
    ``provider`` is already installed.
    """
    current: contextvars.ContextVar[Callable[[], Any]] = getattr(module, var_name)
    try:
        default = contextvars.Context().run(current.get)
    except LookupError:
        default = None
    if default is provider and getattr(module, default_name) is provider:
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
    setattr(module, default_name, provider)
    setattr(module, var_name, contextvars.ContextVar(current.name, default=provider))


def setup_deterministic_runtime() -> None:
    """Installs Temporal's deterministic time, id, and random providers for ADK.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    The providers become the process-wide defaults of ADK's
    ``google.adk.platform`` time, uuid, and random seams, so they apply inside
    workflow tasks (which run on worker threads with an empty contextvars
    context) as well as in the calling context. Inside a workflow, time comes
    from ``workflow.time()``, and ids and randoms come from a workflow-private
    deterministic stream (a ``workflow.new_random()`` per run; ids are v4
    UUIDs built from that stream), so ADK-generated ids and retry jitter are
    reproducible on replay without shifting the sequence user code sees from
    ``workflow.random()`` and ``workflow.uuid4()``. In query handlers and
    update validators time is still ``workflow.time()``, while ids and randoms
    come from fresh entropy that leaves the private stream untouched, since
    those are never replayed. Other read-only code, such as a dynamic
    workflow's ``dynamic_config``, is replayed, so drawing an id or random
    there raises :py:class:`temporalio.workflow.ReadOnlyContextError`, as
    ``workflow.random()`` does. Outside a workflow in the same process
    (activities, client code) they fall back to ``time.time()``,
    ``uuid.uuid4()``, and an unseeded ``random.Random()``.

    Overrides through ADK's ``set_*_provider`` functions must be made after
    this runs (after the worker starts, or from workflow code); one made
    earlier is replaced, with a warning. ADK's ``reset_*_provider`` functions
    restore these deterministic providers, not the standard-library ones.

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
            "_default_time_provider",
            _deterministic_time_provider,
        )
        _install_provider(
            google.adk.platform.uuid,
            "_id_provider_context_var",
            "_default_id_provider",
            _deterministic_id_provider,
        )
        _install_provider(
            google.adk.platform._random,
            "_random_provider_context_var",
            "_default_random_provider",
            _deterministic_random_provider,
        )


class GoogleAdkPlugin(SimplePlugin):
    """A Temporal Worker Plugin configured for ADK.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This plugin configures:

    - Pydantic Payload Converter (required for ADK objects).
    - Sandbox Passthrough for google.adk, google.genai, and OpenTelemetry modules.
    - ADK's time, id, and random providers, so ADK-generated ids and retry
      jitter come from the workflow's deterministic clock and a
      workflow-private deterministic random stream
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
                        "google.adk",
                        "google.genai",
                        "mcp",
                        # ADK imports OpenTelemetry context lazily for graph workflows.
                        "opentelemetry",
                        # ADK probes these optional model SDKs lazily on each LLM turn.
                        "anthropic",
                        "litellm",
                        "openai",
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
