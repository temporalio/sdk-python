from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import multiprocessing
import multiprocessing.managers
import threading
import typing
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import opentelemetry.trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import get_current_span, get_tracer

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.contrib.opentelemetry import workflow as otel_workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import SharedStateManager, UnsandboxedWorkflowRunner, Worker


@dataclass(frozen=True)
class SerialisableSpan:
    """A serialisable, incomplete representation of a span for testing purposes."""

    @dataclass(frozen=True)
    class SpanContext:
        trace_id: int
        span_id: int

        @classmethod
        def from_span_context(
            cls, context: opentelemetry.trace.SpanContext
        ) -> "SerialisableSpan.SpanContext":
            return cls(
                trace_id=context.trace_id,
                span_id=context.span_id,
            )

        @classmethod
        def from_optional_span_context(
            cls, context: Optional[opentelemetry.trace.SpanContext]
        ) -> Optional["SerialisableSpan.SpanContext"]:
            if context is None:
                return None
            return cls.from_span_context(context)

    @dataclass(frozen=True)
    class Link:
        context: SerialisableSpan.SpanContext
        attributes: Dict[str, Any]

    name: str
    context: Optional[SpanContext]
    parent: Optional[SpanContext]
    attributes: Dict[str, Any]
    links: Sequence[Link]

    @classmethod
    def from_readable_span(cls, span: ReadableSpan) -> "SerialisableSpan":
        return cls(
            name=span.name,
            context=cls.SpanContext.from_optional_span_context(span.context),
            parent=cls.SpanContext.from_optional_span_context(span.parent),
            attributes=dict(span.attributes or {}),
            links=tuple(
                cls.Link(
                    context=cls.SpanContext.from_span_context(link.context),
                    attributes=dict(span.attributes or {}),
                )
                for link in span.links
            ),
        )


SerialisableSpanListProxy: typing.TypeAlias = multiprocessing.managers.ListProxy[
    SerialisableSpan
]


def make_span_proxy_list(
    manager: multiprocessing.managers.SyncManager,
) -> SerialisableSpanListProxy:
    """Create a list proxy to share `SerialisableSpan` across processes."""
    return manager.list()


class _ListProxySpanExporter(SpanExporter):
    """Implementation of :class:`SpanExporter` that exports spans to a
    list proxy created by a multiprocessing manager.

    This class is used for testing multiprocessing setups, as we can get access
    to the finished spans from the parent process.

    In production, you would use `OTLPSpanExporter` or similar to export spans.
    Tracing is designed to be distributed, the child process can push collected
    spans directly to a collector or backend, which can reassemble the spans
    into a single trace.
    """

    def __init__(self, finished_spans: SerialisableSpanListProxy) -> None:
        self._finished_spans = finished_spans
        self._stopped = False
        self._lock = threading.Lock()

    def export(self, spans: typing.Sequence[ReadableSpan]) -> SpanExportResult:
        if self._stopped:
            return SpanExportResult.FAILURE
        with self._lock:
            # Note: ReadableSpan is not picklable, so convert to a DTO
            # Note: we could use `span.to_json()` but there isn't a `from_json`
            # and the serialisation isn't easily reversible, e.g. `parent` context
            # is lost, span/trace IDs are transformed into strings
            self._finished_spans.extend(
                [SerialisableSpan.from_readable_span(span) for span in spans]
            )
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self._stopped = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def dump_spans(
    spans: Iterable[Union[ReadableSpan, SerialisableSpan]],
    *,
    parent_id: Optional[int] = None,
    with_attributes: bool = True,
    indent_depth: int = 0,
) -> List[str]:
    ret: List[str] = []
    for span in spans:
        if (not span.parent and parent_id is None) or (
            span.parent and span.parent.span_id == parent_id
        ):
            span_str = f"{'  ' * indent_depth}{span.name}"
            if with_attributes:
                span_str += f" (attributes: {dict(span.attributes or {})})"
            # Add links
            if span.links:
                span_links: List[str] = []
                for link in span.links:
                    for link_span in spans:
                        if (
                            link_span.context
                            and link_span.context.span_id == link.context.span_id
                        ):
                            span_links.append(link_span.name)
                span_str += f" (links: {', '.join(span_links)})"
            # Signals can duplicate in rare situations, so we make sure not to
            # re-add
            if "Signal" in span_str and span_str in ret:
                continue
            ret.append(span_str)
            ret += dump_spans(
                spans,
                parent_id=(span.context.span_id if span.context else None),
                with_attributes=with_attributes,
                indent_depth=indent_depth + 1,
            )
    return ret
