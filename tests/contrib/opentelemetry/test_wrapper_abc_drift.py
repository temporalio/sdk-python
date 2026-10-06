"""Drift guard: replay-safe wrappers must override the full public surface of
the OpenTelemetry ABCs they subclass.

OpenTelemetry adds new API surface to its ABCs as non-abstract no-op defaults
(e.g. ``Meter.create_gauge`` in 1.23, ``Span.add_link`` in 1.23). Because
those defaults exist on the class, ``__getattr__`` delegation cannot intercept
them, so any method a wrapper does not explicitly override silently no-ops
instead of delegating to the wrapped instance -- swallowing telemetry for
every caller. These tests enumerate the installed ABCs by reflection so a new
opentelemetry-api release that grows surface fails loudly (notably in the
latest-dependency CI job) instead of degrading silently.
"""

import inspect

import pytest
from opentelemetry._logs import Logger, LoggerProvider
from opentelemetry.metrics import (
    Counter,
    Histogram,
    Meter,
    MeterProvider,
    UpDownCounter,
    _Gauge,
)
from opentelemetry.trace import Span, Tracer, TracerProvider

from temporalio.contrib.opentelemetry import (
    ReplaySafeLoggerProvider,
    ReplaySafeMeterProvider,
    ReplaySafeTracerProvider,
)
from temporalio.contrib.opentelemetry._logger_provider import _ReplaySafeLogger
from temporalio.contrib.opentelemetry._meter_provider import (
    _ReplaySafeCounter,
    _ReplaySafeGauge,
    _ReplaySafeHistogram,
    _ReplaySafeMeter,
    _ReplaySafeUpDownCounter,
)
from temporalio.contrib.opentelemetry._tracer_provider import (
    _ReplaySafeSpan,
    _ReplaySafeTracer,
)


def _public_surface(abc: type) -> set[str]:
    """Method names a wrapper must cover: every public callable attribute of
    the ABC (which includes non-abstract no-op defaults) plus every non-dunder
    abstract method. Properties are excluded: they carry instrumentation-scope
    metadata, not telemetry, and inheriting them is safe."""
    surface = {
        name
        for name in dir(abc)
        if not name.startswith("_") and callable(inspect.getattr_static(abc, name))
    }
    surface.update(
        name
        for name in getattr(abc, "__abstractmethods__", ())
        if not name.startswith("__")
    )
    return surface


# (wrapper, wrapped OTel ABC, intentionally inherited members). An entry in
# the third element is an explicit opt-out: add a name there only when
# inheriting the OTel default is deliberate, and say why in a comment.
_WRAPPER_CASES: list[tuple[type, type, frozenset[str]]] = [
    (_ReplaySafeMeter, Meter, frozenset()),
    (ReplaySafeMeterProvider, MeterProvider, frozenset()),
    (_ReplaySafeCounter, Counter, frozenset()),
    (_ReplaySafeUpDownCounter, UpDownCounter, frozenset()),
    (_ReplaySafeHistogram, Histogram, frozenset()),
    (_ReplaySafeGauge, _Gauge, frozenset()),
    (_ReplaySafeLogger, Logger, frozenset()),
    (ReplaySafeLoggerProvider, LoggerProvider, frozenset()),
    (_ReplaySafeTracer, Tracer, frozenset()),
    (ReplaySafeTracerProvider, TracerProvider, frozenset()),
    (_ReplaySafeSpan, Span, frozenset()),
]


@pytest.mark.parametrize(
    ("wrapper", "abc", "intentionally_inherited"),
    _WRAPPER_CASES,
    ids=[wrapper.__name__ for wrapper, _, _ in _WRAPPER_CASES],
)
def test_wrapper_overrides_full_otel_abc_surface(
    wrapper: type, abc: type, intentionally_inherited: frozenset[str]
):
    surface = _public_surface(abc)
    assert surface, (
        f"reflection found no public methods on {abc.__module__}.{abc.__qualname__};"
        " the drift guard would be vacuous"
    )

    missing = surface - set(vars(wrapper)) - intentionally_inherited
    assert not missing, (
        f"{wrapper.__name__} inherits {sorted(missing)} from"
        f" {abc.__module__}.{abc.__qualname__} without overriding them. Inherited"
        " OTel defaults no-op instead of delegating to the wrapped instance,"
        " silently dropping telemetry for all callers. Override each method"
        " (delegate, wrap, or replay-gate as appropriate) or add it to this"
        " test's intentionally-inherited allowlist with a comment."
    )

    stale = intentionally_inherited - surface
    assert not stale, (
        f"allowlist entries {sorted(stale)} for {wrapper.__name__} are not on"
        f" {abc.__module__}.{abc.__qualname__} anymore; remove them"
    )
