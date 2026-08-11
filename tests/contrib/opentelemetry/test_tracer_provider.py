"""Tests for ReplaySafeTracerProvider."""

from __future__ import annotations

from opentelemetry.trace import NoOpTracer, Tracer
from opentelemetry.util.types import Attributes

from temporalio.contrib.opentelemetry import create_tracer_provider


def test_replay_safe_tracer_provider_supports_older_otel_signatures():
    """The get_tracer attributes parameter (opentelemetry 1.26) must only be
    forwarded when set, so providers with the older three-parameter signature
    (e.g. the OpenTelemetry SDK at the declared 1.24 floor) keep working."""
    provider = create_tracer_provider()
    calls: list[tuple[str, str | None, str | None]] = []

    def pre_126_get_tracer(
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
    ) -> Tracer:
        calls.append(
            (instrumenting_module_name, instrumenting_library_version, schema_url)
        )
        return NoOpTracer()

    setattr(provider._tracer_provider, "get_tracer", pre_126_get_tracer)

    provider.get_tracer("mod", "1.0", "https://schema")

    assert calls == [("mod", "1.0", "https://schema")]


def test_replay_safe_tracer_provider_forwards_attributes_when_set():
    """When the caller sets attributes, they are forwarded to the wrapped
    provider (1.26+ signature)."""
    provider = create_tracer_provider()
    seen: list[tuple[str, str | None, str | None, Attributes | None]] = []

    def recording_get_tracer(
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes | None = None,
    ) -> Tracer:
        seen.append(
            (
                instrumenting_module_name,
                instrumenting_library_version,
                schema_url,
                attributes,
            )
        )
        return NoOpTracer()

    setattr(provider._tracer_provider, "get_tracer", recording_get_tracer)

    provider.get_tracer("mod", attributes={"k": "v"})

    assert seen == [("mod", None, None, {"k": "v"})]
