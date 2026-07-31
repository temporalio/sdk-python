"""Tests for replay-safe handling of ADK's OpenTelemetry metrics.

Google ADK records token-usage/latency metrics through the process-global
OpenTelemetry meter (scope "gcp.vertex.agent") from code that runs
workflow-side under the Temporal adapter. Without gating, 1 real execution +
N replays produces (1 + N) observations per instrument, while the activity
(the model / tool call) runs exactly once. Installing
ReplaySafeMeterProvider as the global meter provider suppresses the replay
recordings while leaving first-execution recordings intact.
"""

import uuid
import warnings
from collections.abc import AsyncGenerator
from datetime import timedelta

import opentelemetry.metrics
import pytest
from google.adk import Agent
from google.adk.models import BaseLlm, LLMRegistry
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import set_tracer_provider

import temporalio.contrib.google_adk_agents.workflow
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowHistory
from temporalio.contrib.google_adk_agents import GoogleAdkPlugin, TemporalModel
from temporalio.contrib.opentelemetry import (
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
        weather_tool = temporalio.contrib.google_adk_agents.workflow.activity_tool(
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


async def _run_once_and_replay(
    client: Client, num_replays: int
) -> tuple[int, WorkflowHistory]:
    """Run the agent workflow once for real, then replay it num_replays times.

    Returns the number of real activity executions observed for this run.
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

    for _ in range(num_replays):
        await Replayer(
            workflows=[ReplayMetricsAgent],
            plugins=[GoogleAdkPlugin()],
        ).replay_workflow(history)

    return activity_executions - activity_executions_before, history


async def test_replay_safe_meter_provider_suppresses_replay_metrics(
    client: Client,
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
):
    reader = InMemoryMetricReader()
    opentelemetry.metrics.set_meter_provider(
        ReplaySafeMeterProvider(MeterProvider(metric_readers=[reader]))
    )

    real_executions, _ = await _run_once_and_replay(client, num_replays=3)

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

    real_executions, _ = await _run_once_and_replay(client, num_replays=3)

    assert real_executions == 1
    assert adk_metric_counts(reader) == {
        name: count * (1 + 3) for name, count in EXPECTED_BASELINE.items()
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


def test_plugin_does_not_warn_with_replay_safe_providers(
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
    reset_otel_tracer_provider,  # type: ignore[reportUnusedParameter]
):
    opentelemetry.metrics.set_meter_provider(ReplaySafeMeterProvider(MeterProvider()))
    set_tracer_provider(create_tracer_provider())
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        GoogleAdkPlugin().configure_worker(_worker_config())
    assert not [w for w in recorded if "replay-safe" in str(w.message)]


def test_plugin_does_not_warn_with_unset_providers(
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
    reset_otel_tracer_provider,  # type: ignore[reportUnusedParameter]
):
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        GoogleAdkPlugin().configure_worker(_worker_config())
    assert not [w for w in recorded if "replay-safe" in str(w.message)]
