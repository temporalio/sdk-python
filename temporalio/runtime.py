"""Runtime for clients and workers. (experimental)

This module is currently experimental. The API may change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import ClassVar, Mapping, Optional, Union

import temporalio.bridge.metric
import temporalio.bridge.runtime
import temporalio.common

_default_runtime: Optional[Runtime] = None


class Runtime:
    """Runtime for Temporal Python SDK.

    Users are encouraged to use :py:meth:`default`. It can be set with
    :py:meth:`set_default`. Every time a new runtime is created, a new internal
    thread pool is created.
    """

    @staticmethod
    def default() -> Runtime:
        """Get the default runtime, creating if not already created.

        If the default runtime needs to be different, it should be done with
        :py:meth:`set_default` before this is called or ever used.

        Returns:
            The default runtime.
        """
        global _default_runtime
        if not _default_runtime:
            _default_runtime = Runtime(telemetry=TelemetryConfig())
        return _default_runtime

    @staticmethod
    def set_default(runtime: Runtime, *, error_if_already_set: bool = True) -> None:
        """Set the default runtime to the given runtime.

        This should be called before any Temporal client is created, but can
        change the existing one. Any clients and workers created with the
        previous runtime will stay on that runtime.

        Args:
            runtime: The runtime to set.
            error_if_already_set: If True and default is already set, this will
                raise a RuntimeError.
        """
        global _default_runtime
        if _default_runtime and error_if_already_set:
            raise RuntimeError("Runtime default already set")
        _default_runtime = runtime

    def __init__(self, *, telemetry: TelemetryConfig) -> None:
        """Create a default runtime with the given telemetry config.

        Each new runtime creates a new internal thread pool, so use sparingly.
        """
        self._core_runtime = temporalio.bridge.runtime.Runtime(
            telemetry=telemetry._to_bridge_config()
        )
        core_meter = temporalio.bridge.metric.MetricMeter.create(self._core_runtime)
        if not core_meter:
            self._metric_meter = temporalio.common.MetricMeter.noop
        else:
            self._metric_meter = _MetricMeter(core_meter, core_meter.default_attributes)

    @property
    def metric_meter(self) -> temporalio.common.MetricMeter:
        """Metric meter for this runtime. This is a no-op metric meter if no
        metrics were configured.
        """
        return self._metric_meter


@dataclass
class TelemetryFilter:
    """Filter for telemetry use."""

    core_level: str
    """Level for Core. Can be ``ERROR``, ``WARN``, ``INFO``, ``DEBUG``, or
    ``TRACE``.
    """

    other_level: str
    """Level for non-Core. Can be ``ERROR``, ``WARN``, ``INFO``, ``DEBUG``, or
    ``TRACE``.
    """

    def formatted(self) -> str:
        """Return a formatted form of this filter."""
        # We intentionally aren't using __str__ or __format__ so they can keep
        # their original dataclass impls
        return f"{self.other_level},temporal_sdk_core={self.core_level},temporal_client={self.core_level},temporal_sdk={self.core_level}"


@dataclass(frozen=True)
class LoggingConfig:
    """Configuration for runtime logging."""

    filter: Union[TelemetryFilter, str]
    """Filter for logging. Can use :py:class:`TelemetryFilter` or raw string."""

    default: ClassVar[LoggingConfig]
    """Default logging configuration of Core WARN level and other ERROR
    level.
    """

    def _to_bridge_config(self) -> temporalio.bridge.runtime.LoggingConfig:
        return temporalio.bridge.runtime.LoggingConfig(
            filter=self.filter
            if isinstance(self.filter, str)
            else self.filter.formatted(),
            # Log forwarding not currently supported in Python
            forward=False,
        )


LoggingConfig.default = LoggingConfig(
    filter=TelemetryFilter(core_level="WARN", other_level="ERROR")
)


class OpenTelemetryMetricTemporality(Enum):
    """Temporality for OpenTelemetry metrics."""

    CUMULATIVE = 1
    DELTA = 2


@dataclass(frozen=True)
class OpenTelemetryConfig:
    """Configuration for OpenTelemetry collector."""

    url: str
    headers: Optional[Mapping[str, str]] = None
    metric_periodicity: Optional[timedelta] = None
    metric_temporality: OpenTelemetryMetricTemporality = (
        OpenTelemetryMetricTemporality.CUMULATIVE
    )

    def _to_bridge_config(self) -> temporalio.bridge.runtime.OpenTelemetryConfig:
        return temporalio.bridge.runtime.OpenTelemetryConfig(
            url=self.url,
            headers=self.headers or {},
            metric_periodicity_millis=None
            if not self.metric_periodicity
            else round(self.metric_periodicity.total_seconds() * 1000),
            metric_temporality_delta=self.metric_temporality
            == OpenTelemetryMetricTemporality.DELTA,
        )


@dataclass(frozen=True)
class PrometheusConfig:
    """Configuration for Prometheus metrics endpoint."""

    bind_address: str
    counters_total_suffix: bool = False
    unit_suffix: bool = False

    def _to_bridge_config(self) -> temporalio.bridge.runtime.PrometheusConfig:
        return temporalio.bridge.runtime.PrometheusConfig(
            bind_address=self.bind_address,
            counters_total_suffix=self.counters_total_suffix,
            unit_suffix=self.unit_suffix,
        )


@dataclass(frozen=True)
class TelemetryConfig:
    """Configuration for Core telemetry."""

    logging: Optional[LoggingConfig] = LoggingConfig.default
    """Logging configuration."""

    metrics: Optional[Union[OpenTelemetryConfig, PrometheusConfig]] = None
    """Metrics configuration."""

    global_tags: Mapping[str, str] = field(default_factory=dict)
    """OTel resource tags to be applied to all metrics."""

    attach_service_name: bool = True
    """Whether to put the service_name on every metric."""

    metric_prefix: Optional[str] = None
    """Prefix to put on every Temporal metric. If unset, defaults to
    ``temporal_``."""

    def _to_bridge_config(self) -> temporalio.bridge.runtime.TelemetryConfig:
        return temporalio.bridge.runtime.TelemetryConfig(
            logging=None if not self.logging else self.logging._to_bridge_config(),
            metrics=None
            if not self.metrics
            else temporalio.bridge.runtime.MetricsConfig(
                opentelemetry=None
                if not isinstance(self.metrics, OpenTelemetryConfig)
                else self.metrics._to_bridge_config(),
                prometheus=None
                if not isinstance(self.metrics, PrometheusConfig)
                else self.metrics._to_bridge_config(),
                attach_service_name=self.attach_service_name,
                global_tags=self.global_tags or None,
                metric_prefix=self.metric_prefix,
            ),
        )


class _MetricMeter(temporalio.common.MetricMeter):
    def __init__(
        self,
        core_meter: temporalio.bridge.metric.MetricMeter,
        core_attrs: temporalio.bridge.metric.MetricAttributes,
    ) -> None:
        self._core_meter = core_meter
        self._core_attrs = core_attrs

    def create_counter(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> temporalio.common.MetricCounter:
        return _MetricCounter(
            name,
            description,
            unit,
            temporalio.bridge.metric.MetricCounter(
                self._core_meter, name, description, unit
            ),
            self._core_attrs,
        )

    def create_histogram(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> temporalio.common.MetricHistogram:
        return _MetricHistogram(
            name,
            description,
            unit,
            temporalio.bridge.metric.MetricHistogram(
                self._core_meter, name, description, unit
            ),
            self._core_attrs,
        )

    def create_gauge(
        self, name: str, description: Optional[str] = None, unit: Optional[str] = None
    ) -> temporalio.common.MetricGauge:
        return _MetricGauge(
            name,
            description,
            unit,
            temporalio.bridge.metric.MetricGauge(
                self._core_meter, name, description, unit
            ),
            self._core_attrs,
        )

    def with_additional_attributes(
        self, additional_attributes: temporalio.common.MetricAttributes
    ) -> temporalio.common.MetricMeter:
        return _MetricMeter(
            self._core_meter,
            self._core_attrs.with_additional_attributes(additional_attributes),
        )


class _MetricCounter(temporalio.common.MetricCounter):
    def __init__(
        self,
        name: str,
        description: Optional[str],
        unit: Optional[str],
        core_metric: temporalio.bridge.metric.MetricCounter,
        core_attrs: temporalio.bridge.metric.MetricAttributes,
    ) -> None:
        self._name = name
        self._description = description
        self._unit = unit
        self._core_metric = core_metric
        self._core_attrs = core_attrs

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> Optional[str]:
        return self._description

    @property
    def unit(self) -> Optional[str]:
        return self._unit

    def add(
        self,
        value: int,
        additional_attributes: Optional[temporalio.common.MetricAttributes] = None,
    ) -> None:
        if value < 0:
            raise ValueError("Metric value cannot be negative")
        core_attrs = self._core_attrs
        if additional_attributes:
            core_attrs = core_attrs.with_additional_attributes(additional_attributes)
        self._core_metric.add(value, core_attrs)

    def with_additional_attributes(
        self, additional_attributes: temporalio.common.MetricAttributes
    ) -> temporalio.common.MetricCounter:
        return _MetricCounter(
            self._name,
            self._description,
            self._unit,
            self._core_metric,
            self._core_attrs.with_additional_attributes(additional_attributes),
        )


class _MetricHistogram(temporalio.common.MetricHistogram):
    def __init__(
        self,
        name: str,
        description: Optional[str],
        unit: Optional[str],
        core_metric: temporalio.bridge.metric.MetricHistogram,
        core_attrs: temporalio.bridge.metric.MetricAttributes,
    ) -> None:
        self._name = name
        self._description = description
        self._unit = unit
        self._core_metric = core_metric
        self._core_attrs = core_attrs

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> Optional[str]:
        return self._description

    @property
    def unit(self) -> Optional[str]:
        return self._unit

    def record(
        self,
        value: int,
        additional_attributes: Optional[temporalio.common.MetricAttributes] = None,
    ) -> None:
        if value < 0:
            raise ValueError("Metric value cannot be negative")
        core_attrs = self._core_attrs
        if additional_attributes:
            core_attrs = core_attrs.with_additional_attributes(additional_attributes)
        self._core_metric.record(value, core_attrs)

    def with_additional_attributes(
        self, additional_attributes: temporalio.common.MetricAttributes
    ) -> temporalio.common.MetricHistogram:
        return _MetricHistogram(
            self._name,
            self._description,
            self._unit,
            self._core_metric,
            self._core_attrs.with_additional_attributes(additional_attributes),
        )


class _MetricGauge(temporalio.common.MetricGauge):
    def __init__(
        self,
        name: str,
        description: Optional[str],
        unit: Optional[str],
        core_metric: temporalio.bridge.metric.MetricGauge,
        core_attrs: temporalio.bridge.metric.MetricAttributes,
    ) -> None:
        self._name = name
        self._description = description
        self._unit = unit
        self._core_metric = core_metric
        self._core_attrs = core_attrs

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> Optional[str]:
        return self._description

    @property
    def unit(self) -> Optional[str]:
        return self._unit

    def set(
        self,
        value: int,
        additional_attributes: Optional[temporalio.common.MetricAttributes] = None,
    ) -> None:
        if value < 0:
            raise ValueError("Metric value cannot be negative")
        core_attrs = self._core_attrs
        if additional_attributes:
            core_attrs = core_attrs.with_additional_attributes(additional_attributes)
        self._core_metric.set(value, core_attrs)

    def with_additional_attributes(
        self, additional_attributes: temporalio.common.MetricAttributes
    ) -> temporalio.common.MetricGauge:
        return _MetricGauge(
            self._name,
            self._description,
            self._unit,
            self._core_metric,
            self._core_attrs.with_additional_attributes(additional_attributes),
        )
