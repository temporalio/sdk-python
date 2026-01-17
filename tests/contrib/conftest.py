"""Shared fixtures for contrib tests."""

import pytest
import opentelemetry.trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


# Global provider - only initialized once per process to avoid conflicts
_provider: TracerProvider | None = None
_exporter: InMemorySpanExporter | None = None


def _setup_tracing() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Setup shared OTEL tracing. Only initializes once per process."""
    global _provider, _exporter

    if _provider is None:
        _provider = TracerProvider()
        _exporter = InMemorySpanExporter()
        _provider.add_span_processor(SimpleSpanProcessor(_exporter))
        opentelemetry.trace.set_tracer_provider(_provider)

    return _provider, _exporter


@pytest.fixture
def tracing() -> InMemorySpanExporter:
    """Provide an in-memory span exporter for OTEL tracing tests.

    Clears spans before and after each test to ensure isolation.
    """
    _, exporter = _setup_tracing()
    exporter.clear()
    yield exporter
    exporter.clear()


@pytest.fixture
def tracer_provider_and_exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    """Provide both tracer provider and exporter.

    Clears spans before and after each test.
    """
    provider, exporter = _setup_tracing()
    exporter.clear()
    yield provider, exporter
    exporter.clear()
