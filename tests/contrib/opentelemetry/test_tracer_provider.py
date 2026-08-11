"""Tests for ReplaySafeTracerProvider."""

from __future__ import annotations

from opentelemetry.trace import NoOpTracer, Tracer
from opentelemetry.util.types import Attributes

from temporalio.contrib.opentelemetry import create_tracer_provider


def test_replay_safe_tracer_provider_delegates_get_tracer_arguments():
    """get_tracer forwards all arguments, including attributes (1.26+), to
    the wrapped provider."""
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

    provider.get_tracer("mod", "1.0", "https://schema", {"k": "v"})

    assert seen == [("mod", "1.0", "https://schema", {"k": "v"})]
