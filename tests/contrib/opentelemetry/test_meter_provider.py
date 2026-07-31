"""Unit tests for ReplaySafeMeterProvider outside workflows."""

import subprocess
import sys
import textwrap
from collections.abc import Iterable

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
    """Newer opentelemetry-api parameters (get_meter attributes, 1.26; sync
    instrument context, 1.28; create_histogram
    explicit_bucket_boundaries_advisory, 1.30) must only be forwarded when
    set, so providers with older signatures keep working."""

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
    meter.create_counter("counter").add(2, {"attr": "val"})

    assert inner_provider.get_meter_calls == [("test-meter", None, None)]
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


def _run_in_subprocess(code: str) -> None:
    # Import-time behavior must be tested in a fresh interpreter so the
    # simulated old opentelemetry-api is seen before temporalio imports it and
    # no module state leaks into other tests.
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"


def test_module_imports_without_sync_gauge():
    """opentelemetry-api 1.12 through 1.22 has no opentelemetry.metrics._Gauge;
    the module must still import and wrap the other instruments."""
    _run_in_subprocess(
        """
        import opentelemetry.metrics
        from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        del opentelemetry.metrics._Gauge

        from temporalio.contrib.opentelemetry import ReplaySafeMeterProvider

        reader = InMemoryMetricReader()
        provider = ReplaySafeMeterProvider(SdkMeterProvider(metric_readers=[reader]))
        provider.get_meter("test-meter").create_counter("counter").add(1)

        data = reader.get_metrics_data()
        assert data is not None
        metrics = [
            metric.name
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for metric in sm.metrics
        ]
        assert metrics == ["counter"], metrics
        """
    )


def test_tracing_importable_without_metrics_api():
    """opentelemetry-api < 1.12 has no opentelemetry.metrics module at all;
    tracing users must be unaffected and ReplaySafeMeterProvider must raise an
    actionable error on access. Blocking opentelemetry.metrics itself would
    also break the modern opentelemetry-sdk trace module installed here, so
    simulate by failing the guarded submodule import."""
    _run_in_subprocess(
        """
        import sys

        sys.modules["temporalio.contrib.opentelemetry._meter_provider"] = None

        import temporalio.contrib.opentelemetry as otel_contrib

        assert otel_contrib.ReplaySafeTracerProvider is not None
        assert otel_contrib.create_tracer_provider is not None
        try:
            otel_contrib.ReplaySafeMeterProvider
        except ImportError as err:
            assert "opentelemetry-api >= 1.12" in str(err), str(err)
        else:
            raise AssertionError("expected ImportError accessing ReplaySafeMeterProvider")
        """
    )
