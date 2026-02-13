import uuid
from datetime import timedelta
from typing import Any

from agents import Span, Trace, TracingProcessor
from agents.tracing import get_trace_provider

from temporalio.client import Client
from temporalio.contrib.openai_agents.testing import (
    AgentEnvironment,
)
from tests.contrib.openai_agents.test_openai import (
    ResearchWorkflow,
    research_mock_model,
)
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
    async with AgentEnvironment(model=research_mock_model()) as env:
        client = env.applied_on_client(client)

        provider = get_trace_provider()

        processor = MemoryTracingProcessor()
        provider.set_processors([processor])

        async with new_worker(
            client,
            ResearchWorkflow,
        ) as worker:
            workflow_handle = await client.start_workflow(
                ResearchWorkflow.run,
                "Caribbean vacation spots in April, optimizing for surfing, hiking and water sports",
                id=f"research-workflow-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=120),
            )
            await workflow_handle.result()
        print("\n".join([str({"name": t.name}) for t, _ in processor.trace_events]))

        # There are two traces, one is created in the client because it is needed to start the temporal spans
        assert len(processor.trace_events) == 4
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

        print(
            "\n".join(
                [
                    str({"id": t.span_id, "data": t.span_data.export()})
                    for t, _ in processor.span_events
                ]
            )
        )

        # Start workflow traces
        paired_span(processor.span_events[0], processor.span_events[1])
        assert (
            processor.span_events[0][0].span_data.export().get("name")
            == "temporal:startWorkflow:ResearchWorkflow"
        )

        # Execute workflow
        paired_span(processor.span_events[2], processor.span_events[-1])
        assert (
            processor.span_events[2][0].span_data.export().get("name")
            == "temporal:executeWorkflow"
        )

        # Initial planner spans - There are only 3 because we don't make an actual model call
        paired_span(processor.span_events[3], processor.span_events[8])
        assert (
            processor.span_events[3][0].span_data.export().get("name") == "PlannerAgent"
        )

        paired_span(processor.span_events[4], processor.span_events[7])
        assert (
            processor.span_events[4][0].span_data.export().get("name")
            == "temporal:startActivity"
        )

        paired_span(processor.span_events[5], processor.span_events[6])
        assert (
            processor.span_events[5][0].span_data.export().get("name")
            == "temporal:executeActivity"
        )

        for span, start in processor.span_events[9:-7]:
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
        paired_span(processor.span_events[-7], processor.span_events[-2])
        assert (
            processor.span_events[-7][0].span_data.export().get("name") == "WriterAgent"
        )

        paired_span(processor.span_events[-6], processor.span_events[-3])
        assert (
            processor.span_events[-6][0].span_data.export().get("name")
            == "temporal:startActivity"
        )

        paired_span(processor.span_events[-5], processor.span_events[-4])
        assert (
            processor.span_events[-5][0].span_data.export().get("name")
            == "temporal:executeActivity"
        )
