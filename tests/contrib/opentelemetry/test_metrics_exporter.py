from __future__ import annotations

import asyncio
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from temporalio.contrib.opentelemetry import MetricsExporter
from temporalio.runtime import MetricBuffer, Runtime, TelemetryConfig


def _make_runtime_and_exporter(
    **telemetry_kwargs: Any,
) -> tuple[Runtime, MetricBuffer, MeterProvider, InMemoryMetricReader, MetricsExporter]:
    buffer = MetricBuffer(10_000)
    runtime = Runtime(
        telemetry=TelemetryConfig(
            metrics=buffer, attach_service_name=False, **telemetry_kwargs
        )
    )
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    exporter = MetricsExporter(buffer, meter_provider)
    return runtime, buffer, meter_provider, reader, exporter


def _collect_metrics_by_name(reader: InMemoryMetricReader) -> dict[str, Any]:
    data = reader.get_metrics_data()
    result: dict[str, Any] = {}
    if not data:
        return result
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                result[metric.name] = metric
    return result


async def test_metrics_exporter_counter():
    runtime, _, _, reader, exporter = _make_runtime_and_exporter()

    counter = runtime.metric_meter.create_counter(
        "my-counter", "my-counter-desc", "my-counter-unit"
    )
    counter.add(100)
    counter.with_additional_attributes({"foo": "bar"}).add(200)

    exporter._drain_once()

    metrics = _collect_metrics_by_name(reader)
    assert "my-counter" in metrics
    metric = metrics["my-counter"]
    assert metric.description == "my-counter-desc"
    assert metric.unit == "my-counter-unit"
    points = {
        tuple(sorted(p.attributes.items())): p.value for p in metric.data.data_points
    }
    assert points[()] == 100
    assert points[(("foo", "bar"),)] == 200

    # Draining again with no new adds should not change values (counters
    # accumulate deltas across drains, not reset).
    exporter._drain_once()
    metrics = _collect_metrics_by_name(reader)
    points = {
        tuple(sorted(p.attributes.items())): p.value
        for p in metrics["my-counter"].data.data_points
    }
    assert points[()] == 100
    assert points[(("foo", "bar"),)] == 200

    # More adds accumulate on top.
    counter.add(50)
    exporter._drain_once()
    metrics = _collect_metrics_by_name(reader)
    points = {
        tuple(sorted(p.attributes.items())): p.value
        for p in metrics["my-counter"].data.data_points
    }
    assert points[()] == 150


async def test_metrics_exporter_gauge_reflects_latest_value():
    runtime, _, _, reader, exporter = _make_runtime_and_exporter()

    gauge = runtime.metric_meter.create_gauge_float("my-gauge")
    gauge.set(1.5)
    exporter._drain_once()

    metrics = _collect_metrics_by_name(reader)
    points = list(metrics["my-gauge"].data.data_points)
    assert len(points) == 1
    assert points[0].value == 1.5

    # Setting a new value should replace, not add to, the prior one.
    gauge.set(9.5)
    exporter._drain_once()
    metrics = _collect_metrics_by_name(reader)
    points = list(metrics["my-gauge"].data.data_points)
    assert len(points) == 1
    assert points[0].value == 9.5


async def test_metrics_exporter_histogram():
    runtime, _, _, reader, exporter = _make_runtime_and_exporter()

    histogram = runtime.metric_meter.create_histogram_float("my-histogram")
    histogram.record(1.0)
    histogram.record(2.0)
    histogram.record(3.0)
    exporter._drain_once()

    metrics = _collect_metrics_by_name(reader)
    points = list(metrics["my-histogram"].data.data_points)
    assert len(points) == 1
    assert points[0].count == 3
    assert points[0].sum == 6.0


async def test_metrics_exporter_attributes_passthrough():
    runtime, _, _, reader, exporter = _make_runtime_and_exporter()

    counter = runtime.metric_meter.create_counter("attrs-counter")
    counter.with_additional_attributes({"foo": "bar", "baz": 123}).add(1)
    exporter._drain_once()

    metrics = _collect_metrics_by_name(reader)
    points = list(metrics["attrs-counter"].data.data_points)
    assert len(points) == 1
    assert dict(points[0].attributes) == {"foo": "bar", "baz": 123}


async def test_metrics_exporter_buffer_not_attached_raises():
    unattached_buffer = MetricBuffer(10_000)
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    exporter = MetricsExporter(unattached_buffer, meter_provider)

    with pytest.raises(RuntimeError):
        exporter._drain_once()

    with pytest.raises(RuntimeError):
        await exporter.run()


async def test_metrics_exporter_run_shutdown_lifecycle():
    from datetime import timedelta

    runtime, _, _, reader, exporter = _make_runtime_and_exporter()
    exporter._poll_interval = timedelta(milliseconds=10)

    counter = runtime.metric_meter.create_counter("lifecycle-counter")

    async with exporter:
        counter.add(42)
        # Give the background poller a couple of cycles to drain.
        await asyncio.sleep(0.2)

    metrics = _collect_metrics_by_name(reader)
    points = list(metrics["lifecycle-counter"].data.data_points)
    assert points[0].value == 42

    # After exit, the background task must actually be done (not leaked).
    assert exporter._run_task is not None
    assert exporter._run_task.done()


async def test_metrics_exporter_shutdown_before_run_starts_does_not_hang():
    # __aenter__ only schedules run() via ensure_future; it doesn't run it.
    # If the `async with` body never suspends, __aexit__ can call shutdown()
    # (setting the shutdown event) before the run() task has executed at
    # all. run() must still notice that pending shutdown on its first loop
    # check instead of clearing the event and polling forever.
    _, _, _, _, exporter = _make_runtime_and_exporter()

    async def enter_and_exit_immediately() -> None:
        async with exporter:
            pass

    await asyncio.wait_for(enter_and_exit_immediately(), timeout=5)

    assert exporter._run_task is not None
    assert exporter._run_task.done()


async def test_metrics_exporter_error_in_one_update_does_not_block_others():
    runtime, _, _, reader, exporter = _make_runtime_and_exporter()

    good_counter = runtime.metric_meter.create_counter("good-counter")
    bad_counter = runtime.metric_meter.create_counter("bad-counter")
    good_counter.add(1)
    bad_counter.add(1)

    errors: list[Exception] = []
    exporter._on_error = errors.append

    original_apply_update = exporter._apply_update

    def flaky_apply_update(update: Any) -> None:
        if update.metric.name == "bad-counter":
            raise ValueError("simulated failure applying bad-counter")
        original_apply_update(update)

    exporter._apply_update = flaky_apply_update  # type: ignore[method-assign]
    exporter._drain_once()

    assert len(errors) == 1
    metrics = _collect_metrics_by_name(reader)
    assert "good-counter" in metrics
    assert "bad-counter" not in metrics
