"""Unit tests for ReplaySafeMeterProvider outside workflows."""

from collections.abc import Iterable

from opentelemetry.metrics import (
    CallbackOptions,
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

from temporalio.contrib.opentelemetry import ReplaySafeMeterProvider


def _metric_data_points(reader: InMemoryMetricReader) -> dict[str, list]:
    points: dict[str, list] = {}
    data = reader.get_metrics_data()
    assert data is not None
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
    """Newer opentelemetry-api parameters (get_meter attributes, 1.26;
    create_histogram explicit_bucket_boundaries_advisory, 1.30) must only be
    forwarded when set, so providers with older signatures keep working."""

    class Pre130Meter(NoOpMeter):
        def __init__(self) -> None:
            super().__init__("pre-1.30-meter")
            self.histogram_calls: list[tuple[str, str, str]] = []

        def create_histogram(  # type: ignore[override]
            self,
            name: str,
            unit: str = "",
            description: str = "",
        ) -> Histogram:
            self.histogram_calls.append((name, unit, description))
            return super().create_histogram(name, unit, description)

    class Pre126MeterProvider(NoOpMeterProvider):
        def __init__(self) -> None:
            self.meter = Pre130Meter()
            self.get_meter_calls: list[tuple[str, str | None, str | None]] = []

        def get_meter(  # type: ignore[override]
            self,
            name: str,
            version: str | None = None,
            schema_url: str | None = None,
        ) -> Meter:
            self.get_meter_calls.append((name, version, schema_url))
            return self.meter

    inner_provider = Pre126MeterProvider()
    provider = ReplaySafeMeterProvider(inner_provider)
    meter = provider.get_meter("test-meter")
    meter.create_histogram("histogram").record(1)

    assert inner_provider.get_meter_calls == [("test-meter", None, None)]
    assert inner_provider.meter.histogram_calls == [("histogram", "", "")]


def test_replay_safe_meter_provider_delegates_other_attributes():
    inner_provider = SdkMeterProvider()
    provider = ReplaySafeMeterProvider(inner_provider)
    assert provider.force_flush()
    provider.shutdown()
