"""Tests for replay-safe handling of ADK's OpenTelemetry metrics and log events.

Google ADK records token-usage/latency metrics and emits gen_ai.* log events
through the process-global OpenTelemetry providers (scope "gcp.vertex.agent")
from code that runs workflow-side under the Temporal adapter. Without gating,
1 real execution + N replays produces (1 + N) observations per instrument and
(1 + N) copies of every log event, while the activity (the model / tool call)
runs exactly once. Installing ReplaySafeMeterProvider /
ReplaySafeLoggerProvider as the global providers suppresses the replay
recordings while leaving first-execution recordings intact.
"""

import inspect
import uuid
import warnings
from collections.abc import AsyncGenerator
from datetime import timedelta

import google.adk.telemetry.tracing
import opentelemetry._logs
import opentelemetry.metrics
import pytest
from google.adk import Agent
from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from opentelemetry.metrics import Meter
from opentelemetry.metrics import MeterProvider as ApiMeterProvider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import set_tracer_provider
from opentelemetry.util.types import Attributes

import temporalio.contrib.google_adk_agents.workflow
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.contrib.google_adk_agents import GoogleAdkPlugin, TemporalModel
from temporalio.contrib.opentelemetry import (
    ReplaySafeLoggerProvider,
    ReplaySafeMeterProvider,
    create_tracer_provider,
)
from temporalio.worker import Replayer, Worker, WorkerConfig
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

MODEL_NAME = "replay-metrics-model"
ADK_METER_SCOPE = "gcp.vertex.agent"

# One agent invocation, one tool call, two model calls (tool call + final
# answer), token usage recorded twice per model call (input + output).
EXPECTED_BASELINE = {
    "gen_ai.invoke_agent.duration": 1,
    "gen_ai.invoke_agent.inference_calls": 1,
    "gen_ai.invoke_agent.tool_calls": 1,
    "gen_ai.execute_tool.duration": 1,
    "gen_ai.client.operation.duration": 2,
    "gen_ai.client.token.usage": 4,
}

# Log events per model call: one gen_ai.system.message, one
# gen_ai.user.message per request content (1 for the first call; 3 for the
# second: prompt + tool call + tool response), one gen_ai.choice per result.
EXPECTED_LOG_BASELINE = {
    "gen_ai.system.message": 2,
    "gen_ai.user.message": 4,
    "gen_ai.choice": 2,
}

# Counts real (worker-side) activity executions; replays must not add to it.
activity_executions = 0


@activity.defn
async def replay_metrics_get_weather(city: str) -> str:  # type: ignore[reportUnusedParameter]
    global activity_executions
    activity_executions += 1
    return "Warm and sunny. 17 degrees."


class ReplayMetricsModel(BaseLlm):
    """Scripted model: one tool call, then a final text answer.

    Both responses carry usage_metadata so gen_ai.client.token.usage records.
    """

    @classmethod
    def supported_models(cls) -> list[str]:
        return [MODEL_NAME]

    def _responses(self) -> list[LlmResponse]:
        return [
            LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                args={"city": "New York"},
                                name="replay_metrics_get_weather",
                            )
                        )
                    ],
                ),
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=100,
                    candidates_token_count=25,
                    total_token_count=125,
                ),
            ),
            LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="warm and sunny")],
                ),
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    prompt_token_count=200,
                    candidates_token_count=10,
                    total_token_count=210,
                ),
            ),
        ]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        for response in self._responses():
            if any(content == response.content for content in llm_request.contents):
                continue
            yield response
            return


@workflow.defn
class ReplayMetricsAgent:
    @workflow.run
    async def run(self, prompt: str, model_name: str) -> str | None:
        weather_tool = temporalio.contrib.google_adk_agents.workflow.activity_as_tool(
            replay_metrics_get_weather,
            start_to_close_timeout=timedelta(seconds=60),
        )
        agent = Agent(
            name="replay_metrics_agent",
            model=TemporalModel(model_name),
            tools=[weather_tool],
        )
        runner = InMemoryRunner(agent=agent, app_name="replay_metrics_app")
        session = await runner.session_service.create_session(
            app_name="replay_metrics_app", user_id="test"
        )
        last_text = None
        async with Aclosing(
            runner.run_async(
                user_id="test",
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
            )
        ) as agen:
            async for event in agen:
                if (
                    event.content
                    and event.content.parts
                    and event.content.parts[0].text
                ):
                    last_text = event.content.parts[0].text
        return last_text


def adk_metric_counts(reader: InMemoryMetricReader) -> dict[str, int]:
    """Total observation count per ADK instrument (sum of data-point counts)."""
    counts: dict[str, int] = {}
    data = reader.get_metrics_data()
    if data is None:
        return counts
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            if sm.scope.name != ADK_METER_SCOPE:
                continue
            for metric in sm.metrics:
                for dp in getattr(metric.data, "data_points", []):
                    counts[metric.name] = counts.get(metric.name, 0) + getattr(
                        dp, "count", 1
                    )
    return counts


def adk_log_event_counts(exporter: InMemoryLogRecordExporter) -> dict[str, int]:
    """Emission count per ADK log event name."""
    counts: dict[str, int] = {}
    for log in exporter.get_finished_logs():
        if log.instrumentation_scope is None or (
            log.instrumentation_scope.name != ADK_METER_SCOPE
        ):
            continue
        event_name = log.log_record.event_name
        if event_name:
            counts[event_name] = counts.get(event_name, 0) + 1
    return counts


@pytest.fixture
def reset_adk_proxy_logger():
    """Clear ADK's cached proxy logger binding around tests.

    ADK's module-level otel_logger is a proxy that caches its real logger on
    first emit and never rebinds, even across a later set_logger_provider
    call, so each test must clear the cache for its own provider to receive
    the events.
    """

    def clear() -> None:
        proxy = google.adk.telemetry.tracing.otel_logger
        if hasattr(proxy, "_real_logger"):
            proxy._real_logger = None  # type: ignore[attr-defined]

    clear()
    yield
    clear()


async def _run_once_and_replay(client: Client, num_replays: int) -> int:
    """Run the agent workflow once for real, then replay it num_replays times.

    Returns the number of real activity executions observed for this run.
    Skips the calling test if the live run's history shows a workflow task
    retry: a retried task legitimately re-records live telemetry
    (at-least-once semantics), which would break the exact-count assertions
    the callers make.
    """
    LLMRegistry.register(ReplayMetricsModel)

    new_config = client.config()
    new_config["plugins"] = [GoogleAdkPlugin()]
    client = Client(**new_config)

    activity_executions_before = activity_executions
    task_queue = f"replay-metrics-{uuid.uuid4()}"
    # Deliberately not setting max_cached_workflows=0 so the live run is
    # exactly one real execution with no forced replay per workflow task.
    async with Worker(
        client,
        task_queue=task_queue,
        activities=[replay_metrics_get_weather],
        workflows=[ReplayMetricsAgent],
    ):
        handle = await client.start_workflow(
            ReplayMetricsAgent.run,
            args=["What is the weather in New York?", MODEL_NAME],
            id=f"replay-metrics-{uuid.uuid4()}",
            task_queue=task_queue,
            execution_timeout=timedelta(seconds=60),
        )
        result = await handle.result()
        assert result == "warm and sunny"
        history = await handle.fetch_history()

    if any(
        event.event_type
        in (
            EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED,
            EventType.EVENT_TYPE_WORKFLOW_TASK_TIMED_OUT,
        )
        for event in history.events
    ):
        pytest.skip(
            "Workflow task retried during the live run; exact telemetry "
            "counts require a retry-free history"
        )

    for _ in range(num_replays):
        await Replayer(
            workflows=[ReplayMetricsAgent],
            plugins=[GoogleAdkPlugin()],
        ).replay_workflow(history)

    return activity_executions - activity_executions_before


async def test_replay_safe_meter_provider_suppresses_replay_metrics(
    client: Client,
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
):
    reader = InMemoryMetricReader()
    opentelemetry.metrics.set_meter_provider(
        ReplaySafeMeterProvider(MeterProvider(metric_readers=[reader]))
    )

    real_executions = await _run_once_and_replay(client, num_replays=3)

    # First execution recorded exactly once (not suppressed), replays added
    # zero observations, and the activity never re-executed.
    assert real_executions == 1
    assert adk_metric_counts(reader) == EXPECTED_BASELINE


async def test_replay_metrics_duplicate_without_replay_safe_meter_provider(
    client: Client,
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
):
    # Control: without the wrapper, every replay re-records every
    # workflow-side ADK metric even though nothing really re-executed.
    reader = InMemoryMetricReader()
    opentelemetry.metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))

    real_executions = await _run_once_and_replay(client, num_replays=3)

    assert real_executions == 1
    assert adk_metric_counts(reader) == {
        name: count * (1 + 3) for name, count in EXPECTED_BASELINE.items()
    }


def _in_memory_logger_provider() -> tuple[LoggerProvider, InMemoryLogRecordExporter]:
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    return provider, exporter


async def test_replay_safe_logger_provider_suppresses_replay_log_events(
    client: Client,
    reset_otel_logger_provider,  # type: ignore[reportUnusedParameter]
    reset_adk_proxy_logger,  # type: ignore[reportUnusedParameter]
):
    provider, exporter = _in_memory_logger_provider()
    opentelemetry._logs.set_logger_provider(ReplaySafeLoggerProvider(provider))

    real_executions = await _run_once_and_replay(client, num_replays=3)

    # First execution emitted exactly once (not suppressed), replays added
    # zero log events, and the activity never re-executed.
    assert real_executions == 1
    assert adk_log_event_counts(exporter) == EXPECTED_LOG_BASELINE


async def test_replay_log_events_duplicate_without_replay_safe_logger_provider(
    client: Client,
    reset_otel_logger_provider,  # type: ignore[reportUnusedParameter]
    reset_adk_proxy_logger,  # type: ignore[reportUnusedParameter]
):
    # Control: without the wrapper, every replay re-emits every workflow-side
    # ADK log event even though nothing really re-executed.
    provider, exporter = _in_memory_logger_provider()
    opentelemetry._logs.set_logger_provider(provider)

    real_executions = await _run_once_and_replay(client, num_replays=3)

    assert real_executions == 1
    assert adk_log_event_counts(exporter) == {
        name: count * (1 + 3) for name, count in EXPECTED_LOG_BASELINE.items()
    }


def _worker_config() -> WorkerConfig:
    return WorkerConfig(workflow_runner=SandboxedWorkflowRunner())


def test_plugin_warns_on_non_replay_safe_meter_provider(
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
):
    opentelemetry.metrics.set_meter_provider(MeterProvider())
    with pytest.warns(UserWarning, match="MeterProvider is not replay-safe"):
        GoogleAdkPlugin().configure_worker(_worker_config())


def test_plugin_warns_on_non_replay_safe_tracer_provider(
    reset_otel_tracer_provider,  # type: ignore[reportUnusedParameter]
):
    set_tracer_provider(TracerProvider())
    with pytest.warns(UserWarning, match="TracerProvider is not replay-safe"):
        GoogleAdkPlugin().configure_worker(_worker_config())


def test_plugin_warns_on_replayer_construction(
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
):
    # Replayer replays are exactly where an unsafe global provider re-records
    # telemetry, so the warning must fire there too.
    opentelemetry.metrics.set_meter_provider(MeterProvider())
    with pytest.warns(UserWarning, match="MeterProvider is not replay-safe"):
        Replayer(workflows=[ReplayMetricsAgent], plugins=[GoogleAdkPlugin()])


def test_plugin_does_not_warn_with_replay_safe_providers(
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
    reset_otel_tracer_provider,  # type: ignore[reportUnusedParameter]
    reset_otel_logger_provider,  # type: ignore[reportUnusedParameter]
):
    opentelemetry.metrics.set_meter_provider(ReplaySafeMeterProvider(MeterProvider()))
    set_tracer_provider(create_tracer_provider())
    opentelemetry._logs.set_logger_provider(ReplaySafeLoggerProvider(LoggerProvider()))
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        GoogleAdkPlugin().configure_worker(_worker_config())
    assert not [w for w in recorded if "replay-safe" in str(w.message)]


def test_plugin_does_not_warn_with_unset_providers(
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
    reset_otel_tracer_provider,  # type: ignore[reportUnusedParameter]
    reset_otel_logger_provider,  # type: ignore[reportUnusedParameter]
):
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        GoogleAdkPlugin().configure_worker(_worker_config())
    assert not [w for w in recorded if "replay-safe" in str(w.message)]


class _DelegatingMeterProvider(ApiMeterProvider):
    """Unknown custom provider delegating to a replay-safe one."""

    def __init__(self) -> None:
        self._inner = ReplaySafeMeterProvider(MeterProvider())

    def get_meter(
        self,
        name: str,
        version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes | None = None,
    ) -> Meter:
        return self._inner.get_meter(name, version, schema_url)


def test_plugin_does_not_warn_on_unknown_custom_provider(
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
    reset_otel_tracer_provider,  # type: ignore[reportUnusedParameter]
):
    # A custom provider delegating to a replay-safe one is a fully replay-safe
    # configuration; an unclassifiable provider must not trigger a false
    # positive.
    opentelemetry.metrics.set_meter_provider(_DelegatingMeterProvider())
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        GoogleAdkPlugin().configure_worker(_worker_config())
    assert not [w for w in recorded if "replay-safe" in str(w.message)]


async def test_plugin_warning_points_at_worker_construction(
    client: Client,
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
    reset_otel_tracer_provider,  # type: ignore[reportUnusedParameter]
):
    # stacklevel on the warning must attribute it to the user's Worker(...)
    # call, i.e. this file, not SDK internals.
    opentelemetry.metrics.set_meter_provider(MeterProvider())
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        Worker(
            client,
            task_queue=f"replay-metrics-{uuid.uuid4()}",
            activities=[replay_metrics_get_weather],
            workflows=[ReplayMetricsAgent],
            plugins=[GoogleAdkPlugin()],
        )
    warned = [
        w for w in recorded if "MeterProvider is not replay-safe" in str(w.message)
    ]
    assert len(warned) == 1
    assert warned[0].category is UserWarning
    assert warned[0].filename == __file__


class _WrappingPlugin:
    """User plugin that delegates to GoogleAdkPlugin through an extra frame."""

    def __init__(self) -> None:
        self._inner = GoogleAdkPlugin()

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        return self._inner.configure_worker(config)


def test_plugin_warning_points_at_wrapping_plugin_caller(
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
    reset_otel_tracer_provider,  # type: ignore[reportUnusedParameter]
):
    # When another plugin wraps GoogleAdkPlugin, the warning must attribute
    # to the nearest user frame (the wrapper's delegation line), not SDK
    # internals or a fixed frame depth.
    opentelemetry.metrics.set_meter_provider(MeterProvider())
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        _WrappingPlugin().configure_worker(_worker_config())
    warned = [
        w for w in recorded if "MeterProvider is not replay-safe" in str(w.message)
    ]
    assert len(warned) == 1
    assert warned[0].filename == __file__
    source_lines, start = inspect.getsourcelines(_WrappingPlugin.configure_worker)
    delegation_line = start + next(
        offset
        for offset, line in enumerate(source_lines)
        if "self._inner.configure_worker" in line
    )
    assert warned[0].lineno == delegation_line
