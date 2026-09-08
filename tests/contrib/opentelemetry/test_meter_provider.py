"""Tests for ReplaySafeMeterProvider."""

import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from unittest.mock import patch

import opentelemetry.metrics
import pytest
from opentelemetry.context import Context
from opentelemetry.metrics import (
    CallbackOptions,
    Counter,
    Histogram,
    Meter,
    MeterProvider,
    NoOpMeter,
    NoOpMeterProvider,
    Observation,
)
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.util.types import Attributes

from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.contrib.opentelemetry import ReplaySafeMeterProvider
from temporalio.worker import UnsandboxedWorkflowRunner
from tests.helpers import assert_eventually, new_worker


def _metric_data_points(reader: InMemoryMetricReader) -> dict[str, list]:
    points: dict[str, list] = {}
    data = reader.get_metrics_data()
    if data is None:
        return points
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                points.setdefault(metric.name, []).extend(metric.data.data_points)
    return points


def test_replay_safe_meter_provider_sync_instruments_pass_through_outside_workflow():
    reader = InMemoryMetricReader()
    provider = ReplaySafeMeterProvider(SdkMeterProvider(metric_readers=[reader]))
    meter = provider.get_meter("test-meter")

    meter.create_counter("counter").add(2, {"attr": "val"})
    meter.create_up_down_counter("up_down_counter").add(-3)
    meter.create_histogram("histogram").record(4)
    meter.create_gauge("gauge").set(5)

    points = _metric_data_points(reader)
    assert points["counter"][0].value == 2
    assert dict(points["counter"][0].attributes) == {"attr": "val"}
    assert points["up_down_counter"][0].value == -3
    assert points["histogram"][0].count == 1
    assert points["histogram"][0].sum == 4
    assert points["gauge"][0].value == 5


def test_replay_safe_meter_provider_observable_instruments_pass_through():
    def callback(options: CallbackOptions) -> Iterable[Observation]:  # type: ignore[reportUnusedParameter]
        return [Observation(10)]

    reader = InMemoryMetricReader()
    provider = ReplaySafeMeterProvider(SdkMeterProvider(metric_readers=[reader]))
    meter = provider.get_meter("test-meter")

    meter.create_observable_counter("observable_counter", callbacks=[callback])
    meter.create_observable_gauge("observable_gauge", callbacks=[callback])
    meter.create_observable_up_down_counter(
        "observable_up_down_counter", callbacks=[callback]
    )

    points = _metric_data_points(reader)
    assert points["observable_counter"][0].value == 10
    assert points["observable_gauge"][0].value == 10
    assert points["observable_up_down_counter"][0].value == 10


def test_replay_safe_meter_provider_delegates_get_meter_arguments():
    class RecordingMeterProvider(MeterProvider):
        def __init__(self) -> None:
            self.calls: list[tuple] = []
            self._inner = SdkMeterProvider()

        def get_meter(
            self,
            name: str,
            version: str | None = None,
            schema_url: str | None = None,
            attributes: Attributes | None = None,
        ) -> Meter:
            self.calls.append((name, version, schema_url, attributes))
            return self._inner.get_meter(name, version, schema_url, attributes)

    inner_provider = RecordingMeterProvider()
    provider = ReplaySafeMeterProvider(inner_provider)
    meter = provider.get_meter(
        "test-meter",
        version="1.2.3",
        schema_url="https://example.com/schema",
        attributes={"attr": "val"},
    )

    assert inner_provider.calls == [
        ("test-meter", "1.2.3", "https://example.com/schema", {"attr": "val"})
    ]
    assert meter.name == "test-meter"
    assert meter.version == "1.2.3"
    assert meter.schema_url == "https://example.com/schema"


def test_replay_safe_meter_provider_supports_older_otel_signatures():
    """Newer opentelemetry-api parameters (sync instrument context, 1.28;
    create_histogram explicit_bucket_boundaries_advisory, 1.30) must only be
    forwarded when set, so providers with older signatures keep working."""

    class Pre128Counter(Counter):
        def __init__(self) -> None:
            self.calls: list[tuple[int | float, Attributes | None]] = []

        def add(  # type: ignore[override]
            self,
            amount: int | float,
            attributes: Attributes | None = None,
        ) -> None:
            self.calls.append((amount, attributes))

    class Pre130Meter(NoOpMeter):
        def __init__(self) -> None:
            super().__init__("pre-1.30-meter")
            self.counter = Pre128Counter()
            self.histogram_calls: list[tuple[str, str, str]] = []

        def create_counter(  # type: ignore[override]
            self,
            name: str,
            unit: str = "",
            description: str = "",
        ) -> Counter:
            return self.counter

        def create_histogram(  # type: ignore[override]
            self,
            name: str,
            unit: str = "",
            description: str = "",
        ) -> Histogram:
            self.histogram_calls.append((name, unit, description))
            return super().create_histogram(name, unit, description)

    class Pre128MeterProvider(NoOpMeterProvider):
        def __init__(self) -> None:
            self.meter = Pre130Meter()
            self.get_meter_calls: list[
                tuple[str, str | None, str | None, Attributes | None]
            ] = []

        def get_meter(  # type: ignore[override]
            self,
            name: str,
            version: str | None = None,
            schema_url: str | None = None,
            attributes: Attributes | None = None,
        ) -> Meter:
            self.get_meter_calls.append((name, version, schema_url, attributes))
            return self.meter

    inner_provider = Pre128MeterProvider()
    provider = ReplaySafeMeterProvider(inner_provider)
    meter = provider.get_meter("test-meter")
    meter.create_histogram("histogram").record(1)
    meter.create_counter("counter").add(2, {"attr": "val"})

    assert inner_provider.get_meter_calls == [("test-meter", None, None, None)]
    assert inner_provider.meter.histogram_calls == [("histogram", "", "")]
    assert inner_provider.meter.counter.calls == [(2, {"attr": "val"})]


def test_replay_safe_meter_provider_forwards_context_when_set():
    class RecordingCounter(Counter):
        def __init__(self) -> None:
            self.calls: list[tuple[int | float, Attributes | None, Context | None]] = []

        def add(
            self,
            amount: int | float,
            attributes: Attributes | None = None,
            context: Context | None = None,
        ) -> None:
            self.calls.append((amount, attributes, context))

    class RecordingMeter(NoOpMeter):
        def __init__(self) -> None:
            super().__init__("recording-meter")
            self.counter = RecordingCounter()

        def create_counter(  # type: ignore[override]
            self,
            name: str,
            unit: str = "",
            description: str = "",
        ) -> Counter:
            return self.counter

    class RecordingProvider(NoOpMeterProvider):
        def __init__(self) -> None:
            self.meter = RecordingMeter()

        def get_meter(  # type: ignore[override]
            self,
            name: str,
            version: str | None = None,
            schema_url: str | None = None,
            attributes: Attributes | None = None,
        ) -> Meter:
            return self.meter

    inner_provider = RecordingProvider()
    provider = ReplaySafeMeterProvider(inner_provider)
    context = Context()
    provider.get_meter("test-meter").create_counter("counter").add(
        3, {"attr": "val"}, context
    )

    assert inner_provider.meter.counter.calls == [(3, {"attr": "val"}, context)]


def test_replay_safe_meter_provider_delegates_other_attributes():
    inner_provider = SdkMeterProvider()
    provider = ReplaySafeMeterProvider(inner_provider)
    assert provider.force_flush()
    provider.shutdown()


@contextmanager
def _workflow_replay_state(*, replaying_history_events: bool) -> Iterator[None]:
    """Simulate workflow context during replay. When replaying_history_events
    is False this is the query/update-validator state: is_replaying() is True
    but is_replaying_history_events() is False."""
    with (
        patch.object(workflow, "in_workflow", return_value=True),
        patch.object(workflow.unsafe, "is_replaying", return_value=True),
        patch.object(
            workflow.unsafe,
            "is_replaying_history_events",
            return_value=replaying_history_events,
        ),
    ):
        yield


def test_replay_safe_meter_provider_drops_recordings_replaying_history_events():
    reader = InMemoryMetricReader()
    provider = ReplaySafeMeterProvider(SdkMeterProvider(metric_readers=[reader]))
    counter = provider.get_meter("test-meter").create_counter("counter")

    with _workflow_replay_state(replaying_history_events=True):
        counter.add(1)

    assert not _metric_data_points(reader).get("counter")


def test_replay_safe_meter_provider_records_from_live_operations_during_replay():
    """Queries and update validators execute at most once per request even
    when the workflow is replaying, so the gate must use
    is_replaying_history_events(), not is_replaying(), and keep their
    recordings."""
    reader = InMemoryMetricReader()
    provider = ReplaySafeMeterProvider(SdkMeterProvider(metric_readers=[reader]))
    counter = provider.get_meter("test-meter").create_counter("counter")

    with _workflow_replay_state(replaying_history_events=False):
        counter.add(1)

    assert _metric_data_points(reader)["counter"][0].value == 1


@workflow.defn
class QueryDuringReplayWorkflow:
    def __init__(self) -> None:
        self._proceed = False

    @workflow.run
    async def run(self) -> None:
        opentelemetry.metrics.get_meter("replay-query-meter").create_counter(
            "run_counter"
        ).add(1)
        await workflow.wait_condition(lambda: self._proceed)

    @workflow.signal
    def proceed(self) -> None:
        self._proceed = True

    @workflow.query
    def query_and_record(self) -> bool:
        opentelemetry.metrics.get_meter("replay-query-meter").create_counter(
            "query_counter"
        ).add(1)
        return workflow.unsafe.is_replaying()


async def test_replay_safe_meter_provider_records_query_during_replay(
    client: Client,
    reset_otel_meter_provider,  # type: ignore[reportUnusedParameter]
):
    """End-to-end check of the replay predicate: with the workflow cache
    disabled, a query forces a full history replay before its handler runs.
    The replayed run() recording must be dropped while the query handler's
    recording -- live, once per request -- must be kept."""
    reader = InMemoryMetricReader()
    provider = ReplaySafeMeterProvider(SdkMeterProvider(metric_readers=[reader]))
    opentelemetry.metrics.set_meter_provider(provider)
    assert opentelemetry.metrics.get_meter_provider() is provider

    async with new_worker(
        client,
        QueryDuringReplayWorkflow,
        # No cache, so the query task must replay history from scratch.
        max_cached_workflows=0,
        # Unsandboxed, so workflow code sees this process's global provider.
        workflow_runner=UnsandboxedWorkflowRunner(),
    ) as worker:
        handle = await client.start_workflow(
            QueryDuringReplayWorkflow.run,
            id=f"query-during-replay-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )

        # Query only after the first workflow task completes: the live run()
        # recording then exists and the evicted workflow must replay it.
        async def first_workflow_task_completed() -> None:
            history = await handle.fetch_history()
            assert any(
                event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED
                for event in history.events
            )

        await assert_eventually(first_workflow_task_completed)

        was_replaying = await handle.query(QueryDuringReplayWorkflow.query_and_record)
        # Pins that the disputed condition was exercised: the handler ran with
        # is_replaying() True, and its recording was still kept below.
        assert was_replaying

        await handle.signal(QueryDuringReplayWorkflow.proceed)
        await handle.result()
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

    points = _metric_data_points(reader)
    # Live run recorded once; the query task's replay of run() was dropped.
    assert points["run_counter"][0].value == 1
    # The query handler's recording during that replay was kept.
    assert points["query_counter"][0].value == 1
