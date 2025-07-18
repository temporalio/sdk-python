import datetime
import uuid
from datetime import timedelta
from typing import Any, Optional

from agents import Span, Trace, TracingProcessor
from agents.tracing import get_trace_provider

from temporalio.client import Client
from temporalio.contrib.openai_agents import (
    ModelActivity,
    OpenAIAgentsTracingInterceptor,
    TestModelProvider,
    set_open_ai_agent_temporal_overrides,
)
from temporalio.contrib.pydantic import pydantic_data_converter
from tests.contrib.openai_agents.test_openai import ResearchWorkflow, TestResearchModel
from tests.helpers import new_worker


class MemoryTracingProcessor(TracingProcessor):
    # True for start events, false for end
    trace_events: list[tuple[Trace, bool]] = []
    span_events: list[tuple[Span, bool]] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.trace_events.append((trace, True))

    def on_trace_end(self, trace: Trace) -> None:
        self.trace_events.append((trace, False))

    def on_span_start(self, span: Span[Any]) -> None:
        self.span_events.append((span, True))

    def on_span_end(self, span: Span[Any]) -> None:
        self.span_events.append((span, False))

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


async def test_tracing(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)

    with set_open_ai_agent_temporal_overrides():
        provider = get_trace_provider()

        processor = MemoryTracingProcessor()
        provider.set_processors([processor])

        model_activity = ModelActivity(TestModelProvider(TestResearchModel()))
        async with new_worker(
            client,
            ResearchWorkflow,
            activities=[model_activity.invoke_model_activity],
            interceptors=[OpenAIAgentsTracingInterceptor()],
        ) as worker:
            workflow_handle = await client.start_workflow(
                ResearchWorkflow.run,
                "Caribbean vacation spots in April, optimizing for surfing, hiking and water sports",
                id=f"research-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=120),
            )
            result = await workflow_handle.result()

        # There is one closed root trace
        assert len(processor.trace_events) == 2
        assert (
            processor.trace_events[0][0].trace_id
            == processor.trace_events[1][0].trace_id
        )
        assert processor.trace_events[0][1]
        assert not processor.trace_events[1][1]

        def paired_span(a: tuple[Span[Any], bool], b: tuple[Span[Any], bool]) -> None:
            assert a[0].trace_id == b[0].trace_id
            assert a[1]
            assert not b[1]

        # Initial planner spans - There are only 3 because we don't make an actual model call
        paired_span(processor.span_events[0], processor.span_events[5])
        assert (
            processor.span_events[0][0].span_data.export().get("name") == "PlannerAgent"
        )

        paired_span(processor.span_events[1], processor.span_events[4])
        assert (
            processor.span_events[1][0].span_data.export().get("name")
            == "temporal:startActivity"
        )

        paired_span(processor.span_events[2], processor.span_events[3])
        assert (
            processor.span_events[2][0].span_data.export().get("name")
            == "temporal:executeActivity"
        )

        for span, start in processor.span_events[6:-6]:
            span_data = span.span_data.export()

            # All spans should be closed
            if start:
                assert any(
                    span.span_id == s.span_id and not s_start
                    for (s, s_start) in processor.span_events
                )

            # Start activity is always parented to an agent
            if span_data.get("name") == "temporal:startActivity":
                parents = [
                    s for (s, _) in processor.span_events if s.span_id == span.parent_id
                ]
                assert (
                    len(parents) == 2
                    and parents[0].span_data.export()["type"] == "agent"
                )

            # Execute is parented to start
            if span_data.get("name") == "temporal:executeActivity":
                parents = [
                    s for (s, _) in processor.span_events if s.span_id == span.parent_id
                ]
                assert (
                    len(parents) == 2
                    and parents[0].span_data.export()["name"]
                    == "temporal:startActivity"
                )

        # Final writer spans - There are only 3 because we don't make an actual model call
        paired_span(processor.span_events[-6], processor.span_events[-1])
        assert (
            processor.span_events[-6][0].span_data.export().get("name") == "WriterAgent"
        )

        paired_span(processor.span_events[-5], processor.span_events[-2])
        assert (
            processor.span_events[-5][0].span_data.export().get("name")
            == "temporal:startActivity"
        )

        paired_span(processor.span_events[-4], processor.span_events[-3])
        assert (
            processor.span_events[-4][0].span_data.export().get("name")
            == "temporal:executeActivity"
        )
