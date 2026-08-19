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


def test_replay_safe_span_delegates_add_link():
    """add_link must delegate to the wrapped span (it previously inherited
    OTel's no-op default and silently dropped links)."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = create_tracer_provider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("add-link-test")

    linked = tracer.start_span("linked")
    linked.end()
    linked_context = linked.get_span_context()

    span = tracer.start_span("linker")
    span.add_link(linked_context, {"k": "v"})
    span.end()

    exported = {s.name: s for s in exporter.get_finished_spans()}
    links = exported["linker"].links
    assert len(links) == 1
    assert links[0].context.span_id == linked_context.span_id
    assert links[0].attributes and dict(links[0].attributes) == {"k": "v"}
