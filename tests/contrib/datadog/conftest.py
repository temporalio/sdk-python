"""Shared test helpers for Datadog tracing interceptor tests.

The ``event_loop``, ``env``, and ``client`` fixtures used here come from the
top-level ``tests/conftest.py``.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest
from ddtrace.internal.writer.writer import TraceWriter
from ddtrace.trace import tracer as _dd_tracer

from temporalio.client import Client
from temporalio.contrib.datadog import DatadogTracingInterceptor


class _SpanCollector(TraceWriter):
    """Minimal ddtrace-compatible writer that captures emitted span batches in memory.

    Installed as the span aggregator's writer, it records spans that the aggregator
    flushes without forwarding them to a Datadog agent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: list[Any] = []

    def write(self, spans: list[Any] | None = None) -> None:
        if spans:
            with self._lock:
                self._spans.extend(spans)

    def flush_queue(self) -> None:
        pass

    def stop(self, timeout: float | None = None) -> None:
        pass

    def recreate(
        self, appsec_enabled: bool | None = None, llmobs_enabled: bool | None = None
    ) -> _SpanCollector:
        return self

    @property
    def spans(self) -> list[Any]:
        with self._lock:
            return list(self._spans)

    def by_op(self, op: str) -> list[Any]:
        target = f"temporal.{op}"
        return [s for s in self.spans if s.name == target]

    def one(self, op: str) -> Any:
        found = self.by_op(op)
        assert len(found) == 1, (
            f"Expected exactly 1 '{op}' span, got {len(found)}: {[s.name for s in found]}"
        )
        return found[0]

    def tag(self, span: Any, key: str) -> Any:
        """Get a Temporal-namespaced tag or metric from a span.

        ddtrace stores integer/float values as *metrics* rather than string
        tags, so this helper checks both storages and returns whichever is set.
        """
        if not key.startswith("temporal."):
            key = "temporal." + key
        value = span.get_tag(key)
        if value is None:
            value = span.get_metric(key)
        return value


@pytest.fixture
def span_collector() -> Any:
    """Replace the global ddtrace writer with an in-memory collector."""
    collector = _SpanCollector()
    orig = _dd_tracer._span_aggregator.writer
    _dd_tracer._span_aggregator.writer = collector
    yield collector
    _dd_tracer._span_aggregator.writer = orig


def _make_interceptor(  # type: ignore[reportUnusedFunction]
    **kwargs: Any,
) -> DatadogTracingInterceptor:
    return DatadogTracingInterceptor(service_name="test-svc", **kwargs)


def _traced_client(  # type: ignore[reportUnusedFunction]
    client: Client, interceptor: DatadogTracingInterceptor
) -> Client:
    """Return a new client that carries the Datadog interceptor.

    The Temporal Worker automatically merges client interceptors into its own
    list, so registering the interceptor only on the client avoids
    double-registration in the worker interceptor chain.
    """
    cfg = client.config()
    cfg["interceptors"] = [interceptor]
    return Client(**cfg)


def _task_queue() -> str:  # type: ignore[reportUnusedFunction]
    return f"dd-test-{uuid.uuid4()}"
