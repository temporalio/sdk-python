"""OpenTelemetry metrics exporter built on :py:class:`temporalio.runtime.MetricBuffer`."""

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta

import opentelemetry.metrics
import opentelemetry.util.types

import temporalio.common
import temporalio.runtime

logger = logging.getLogger(__name__)


@dataclass
class _GaugeState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Keyed by id(BufferedMetricUpdate.attributes) so repeated updates for the
    # same attribute set overwrite rather than accumulate -- gauges are
    # "latest value wins", unlike counters.
    last_values: dict[
        int, tuple[Mapping[str, opentelemetry.util.types.AttributeValue], int | float]
    ] = field(default_factory=dict)


@dataclass
class _Instrument:
    kind: temporalio.runtime.BufferedMetricKind
    counter: opentelemetry.metrics.Counter | None = None
    histogram: opentelemetry.metrics.Histogram | None = None
    gauge_state: "_GaugeState | None" = None


def _make_gauge_callback(
    state: _GaugeState,
) -> Callable[
    [opentelemetry.metrics.CallbackOptions],
    Iterable[opentelemetry.metrics.Observation],
]:
    # OTel invokes observable-gauge callbacks from its own export thread, which
    # runs concurrently with the drain loop mutating last_values -- hence the
    # lock on both sides.
    def callback(
        options: opentelemetry.metrics.CallbackOptions,
    ) -> Iterable[opentelemetry.metrics.Observation]:
        with state.lock:
            return [
                opentelemetry.metrics.Observation(value, attributes=dict(attrs))
                for attrs, value in state.last_values.values()
            ]

    return callback


class MetricsExporter:
    """Exports metrics recorded via a :py:class:`temporalio.runtime.MetricBuffer`
    to an OpenTelemetry :py:class:`opentelemetry.metrics.MeterProvider`.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    This must be started (via :py:meth:`run` or ``async with``) after the
    :py:class:`temporalio.runtime.Runtime` referencing ``buffer`` has been
    constructed, and it must be kept running for as long as metrics should be
    exported. It drains the buffer on a fixed interval; per
    :py:class:`temporalio.runtime.MetricBuffer`, updates are dropped (with an
    error logged by Core) if the buffer is not drained regularly.

    Example:

    .. code-block:: python

        from datetime import timedelta

        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import (
            ConsoleMetricExporter,
            PeriodicExportingMetricReader,
        )

        from temporalio.client import Client
        from temporalio.contrib.opentelemetry import MetricsExporter
        from temporalio.runtime import MetricBuffer, Runtime, TelemetryConfig

        buffer = MetricBuffer(10_000)
        runtime = Runtime(telemetry=TelemetryConfig(metrics=buffer))
        meter_provider = MeterProvider(
            metric_readers=[
                PeriodicExportingMetricReader(
                    ConsoleMetricExporter(), export_interval_millis=5000
                )
            ]
        )

        async with MetricsExporter(buffer, meter_provider):
            client = await Client.connect("localhost:7233", runtime=runtime)
            ...
    """

    def __init__(
        self,
        buffer: temporalio.runtime.MetricBuffer,
        meter_provider: opentelemetry.metrics.MeterProvider | None = None,
        *,
        meter_name: str = "temporalio",
        meter_version: str | None = None,
        poll_interval: timedelta = timedelta(seconds=1),
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Create an exporter that drains ``buffer`` into ``meter_provider``.

        Args:
            buffer: The buffer to drain. Must already be (or about to be) set
                as the ``metrics`` of a
                :py:class:`temporalio.runtime.TelemetryConfig` on a
                constructed :py:class:`temporalio.runtime.Runtime`.
            meter_provider: The provider to create instruments on. Defaults to
                :py:func:`opentelemetry.metrics.get_meter_provider` (the
                global provider) if not given.
            meter_name: Name passed to ``get_meter`` on the provider.
            meter_version: Version passed to ``get_meter`` on the provider.
            poll_interval: How often to drain the buffer. Must be reasonably
                frequent -- see the warning on
                :py:class:`temporalio.runtime.MetricBuffer`.
            on_error: Optional callback invoked (in addition to logging) when
                draining the buffer or applying an individual update fails.
                If this callback itself raises, that is logged and ignored.
        """
        self._buffer = buffer
        self._meter = (
            meter_provider or opentelemetry.metrics.get_meter_provider()
        ).get_meter(meter_name, meter_version)
        self._poll_interval = poll_interval
        self._on_error = on_error
        self._instruments: dict[int, _Instrument] = {}
        self._attributes_cache: dict[
            int, Mapping[str, opentelemetry.util.types.AttributeValue]
        ] = {}
        self._started = False
        self._shutdown_event = asyncio.Event()
        self._run_complete_event = asyncio.Event()
        self._run_task: asyncio.Task | None = None

    async def run(self) -> None:
        """Drain and export on ``poll_interval`` until :py:meth:`shutdown` is called.

        Raises:
            RuntimeError: If ``buffer`` was never attached to a constructed
                :py:class:`temporalio.runtime.Runtime`, or if this is called
                while already running.
        """
        if self._started:
            raise RuntimeError("MetricsExporter is already running")
        self._started = True
        self._shutdown_event.clear()
        self._run_complete_event.clear()
        try:
            while not self._shutdown_event.is_set():
                self._drain_once()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=self._poll_interval.total_seconds(),
                    )
            # Final drain so nothing recorded right before shutdown is lost.
            self._drain_once()
        finally:
            self._started = False
            self._run_complete_event.set()

    async def shutdown(self) -> None:
        """Stop :py:meth:`run` (including a final drain) and wait for it to finish."""
        self._shutdown_event.set()
        await self._run_complete_event.wait()

    async def __aenter__(self) -> "MetricsExporter":
        """Start :py:meth:`run` as a background task."""
        self._run_task = asyncio.ensure_future(self.run())
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Call :py:meth:`shutdown` and await the background task."""
        await self.shutdown()
        if self._run_task:
            await self._run_task

    def _drain_once(self) -> None:
        try:
            updates = self._buffer.retrieve_updates()
        except RuntimeError:
            # Buffer was never attached to a constructed Runtime -- this is a
            # setup error, not a transient failure, so it should propagate
            # rather than spin silently and drop metrics forever.
            raise
        except Exception as err:
            self._handle_error(err)
            return

        for update in updates:
            try:
                self._apply_update(update)
            except Exception as err:
                self._handle_error(err)

    def _handle_error(self, err: Exception) -> None:
        logger.exception("Error exporting buffered Temporal metrics")
        if self._on_error:
            try:
                self._on_error(err)
            except Exception:
                logger.exception("on_error handler failed")

    def _apply_update(self, update: temporalio.runtime.BufferedMetricUpdate) -> None:
        instrument = self._get_or_create_instrument(update.metric)
        attributes = self._get_or_create_attributes(update.attributes)

        if instrument.kind == temporalio.runtime.BUFFERED_METRIC_KIND_COUNTER:
            assert instrument.counter is not None
            instrument.counter.add(update.value, attributes=attributes)
        elif instrument.kind == temporalio.runtime.BUFFERED_METRIC_KIND_HISTOGRAM:
            assert instrument.histogram is not None
            instrument.histogram.record(update.value, attributes=attributes)
        elif instrument.kind == temporalio.runtime.BUFFERED_METRIC_KIND_GAUGE:
            assert instrument.gauge_state is not None
            with instrument.gauge_state.lock:
                instrument.gauge_state.last_values[id(update.attributes)] = (
                    attributes,
                    update.value,
                )
        else:
            raise ValueError(f"Unrecognized buffered metric kind: {instrument.kind}")

    def _get_or_create_instrument(
        self, metric: temporalio.runtime.BufferedMetric
    ) -> _Instrument:
        instrument = self._instruments.get(id(metric))
        if instrument is not None:
            return instrument

        unit = metric.unit or ""
        description = metric.description or ""
        instrument = _Instrument(kind=metric.kind)
        if metric.kind == temporalio.runtime.BUFFERED_METRIC_KIND_COUNTER:
            instrument.counter = self._meter.create_counter(
                metric.name, unit=unit, description=description
            )
        elif metric.kind == temporalio.runtime.BUFFERED_METRIC_KIND_HISTOGRAM:
            instrument.histogram = self._meter.create_histogram(
                metric.name, unit=unit, description=description
            )
        elif metric.kind == temporalio.runtime.BUFFERED_METRIC_KIND_GAUGE:
            gauge_state = _GaugeState()
            instrument.gauge_state = gauge_state
            self._meter.create_observable_gauge(
                metric.name,
                callbacks=[_make_gauge_callback(gauge_state)],
                unit=unit,
                description=description,
            )
        else:
            raise ValueError(f"Unrecognized buffered metric kind: {metric.kind}")

        self._instruments[id(metric)] = instrument
        return instrument

    def _get_or_create_attributes(
        self, attributes: temporalio.common.MetricAttributes
    ) -> Mapping[str, opentelemetry.util.types.AttributeValue]:
        cached = self._attributes_cache.get(id(attributes))
        if cached is not None:
            return cached
        converted: Mapping[str, opentelemetry.util.types.AttributeValue] = dict(
            attributes
        )
        self._attributes_cache[id(attributes)] = converted
        return converted
