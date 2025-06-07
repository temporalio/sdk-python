import uuid
from typing import Any

from agents import SpanData, Trace, TracingProcessor
from agents.tracing import (  # TODO: TraceProvider is not declared in __all__
    TraceProvider,
    get_trace_provider,
)
from agents.tracing.spans import Span

from temporalio import workflow
from temporalio.workflow import ReadOnlyContextError


class ActivitySpanData(SpanData):
    """
    Captures fields from ActivityTaskScheduledEventAttributes for tracing.
    """

    def __init__(
        self,
        activity_id: str,
        activity_type: str,
        task_queue: str,
        schedule_to_close_timeout: float | None = None,
        schedule_to_start_timeout: float | None = None,
        start_to_close_timeout: float | None = None,
        heartbeat_timeout: float | None = None,
    ):
        self.activity_id = activity_id
        self.activity_type = activity_type
        self.task_queue = task_queue
        self.schedule_to_close_timeout = schedule_to_close_timeout
        self.schedule_to_start_timeout = schedule_to_start_timeout
        self.start_to_close_timeout = start_to_close_timeout
        self.heartbeat_timeout = heartbeat_timeout

    @property
    def type(self) -> str:
        return "temporal-activity"

    def export(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "activity_id": self.activity_id,
            "activity_type": self.activity_type,
            "task_queue": self.task_queue,
            "schedule_to_close_timeout": self.schedule_to_close_timeout,
            "schedule_to_start_timeout": self.schedule_to_start_timeout,
            "start_to_close_timeout": self.start_to_close_timeout,
            "heartbeat_timeout": self.heartbeat_timeout,
        }


def activity_span(
    activity_id: str,
    activity_type: str,
    task_queue: str,
    # schedule_to_close_timeout: float,
    # schedule_to_start_timeout: float,
    start_to_close_timeout: float,
    # heartbeat_timeout: float,
) -> Span[ActivitySpanData]:
    return get_trace_provider().create_span(
        span_data=ActivitySpanData(
            activity_id=activity_id,
            activity_type=activity_type,
            task_queue=task_queue,
            # schedule_to_close_timeout=schedule_to_close_timeout,
            # schedule_to_start_timeout=schedule_to_start_timeout,
            start_to_close_timeout=start_to_close_timeout,
            # heartbeat_timeout=heartbeat_timeout,
        ),
        # span_id=span_id,
        # parent=parent,
        # disabled=disabled,
    )


class _TemporalTracingProcessor(TracingProcessor):
    def __init__(self, impl: TracingProcessor):
        super().__init__()
        self._impl = impl

    def on_trace_start(self, trace: Trace) -> None:
        if workflow.in_workflow() and workflow.unsafe.is_replaying():
            # In replay mode, don't report
            return

        self._impl.on_trace_start(trace)

    def on_trace_end(self, trace: Trace) -> None:
        if workflow.in_workflow() and workflow.unsafe.is_replaying():
            # In replay mode, don't report
            return

        self._impl.on_trace_end(trace)

    def on_span_start(self, span: Span[Any]) -> None:
        if workflow.in_workflow() and workflow.unsafe.is_replaying():
            # In replay mode, don't report
            return

        self._impl.on_span_start(span)

    def on_span_end(self, span: Span[Any]) -> None:
        if workflow.in_workflow() and workflow.unsafe.is_replaying():
            # In replay mode, don't report
            return
        self._impl.on_span_end(span)

    def shutdown(self) -> None:
        self._impl.shutdown()

    def force_flush(self) -> None:
        self._impl.force_flush()


class TemporalTraceProvider(TraceProvider):
    """A trace provider that integrates with Temporal workflows."""

    def __init__(self):
        super().__init__()
        self._original_provider = get_trace_provider()
        self._multi_processor = _TemporalTracingProcessor(  # type: ignore[assignment]
            self._original_provider._multi_processor
        )

    def time_iso(self) -> str:
        if workflow.in_workflow():
            """Return the current deterministic time in ISO 8601 format."""
            return workflow.now().isoformat()
        return super().time_iso()

    def gen_trace_id(self) -> str:
        if workflow.in_workflow():
            try:
                """Generate a new trace ID."""
                return f"trace_{workflow.uuid4().hex}"
            except ReadOnlyContextError:
                return f"trace_{uuid.uuid4().hex}"
        return super().gen_trace_id()

    def gen_span_id(self) -> str:
        if workflow.in_workflow():
            try:
                """Generate a new span ID."""
                return f"span_{workflow.uuid4().hex[:24]}"
            except ReadOnlyContextError:
                return f"span_{uuid.uuid4().hex[:24]}"
        return super().gen_span_id()

    def gen_group_id(self) -> str:
        if workflow.in_workflow():
            try:
                """Generate a new group ID."""
                return f"group_{workflow.uuid4().hex[:24]}"
            except ReadOnlyContextError:
                return f"group_{uuid.uuid4().hex[:24]}"
        return super().gen_group_id()

    def __enter__(self):
        # setup code
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # cleanup code
        self._multi_processor.shutdown()
