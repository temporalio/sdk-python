from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import temporalio.activity
import temporalio.workflow
from temporalio.contrib.datadog._constants import TEMPORAL_TAG_PREFIX

# ``ddtrace`` is intentionally not imported at module level. ``WrappedTracer``
# resolves and caches the tracer and Context class in the host process so sandbox
# extern calls do not import ``ddtrace``.


@dataclass(frozen=True)
class FinishContext:
    """Context passed to a user-supplied ``on_span_finish`` callback.

    Attributes:
        operation: The Temporal operation name (e.g. ``"RunWorkflow"``).
        exception: The exception that caused the span to fail, or ``None``.
    """

    operation: str
    exception: BaseException | None


@dataclass(frozen=True)
class FinishResult:
    """Returned by ``on_span_finish`` to control how a span is finished.

    All fields default to leaving the interceptor's default behavior in place;
    return ``None`` from the callback (or omit the callback) for the default.

    Attributes:
        extra_tags: Tags applied to the span before it is finished.
    """

    extra_tags: Mapping[str, Any] | None = None


class WrappedTracer:
    tracer: Any

    def __init__(
        self,
        *,
        service_name: str | None,
        tracer: Any,
        on_span_finish: Callable[[FinishContext], FinishResult | None] | None = None,
        annotator: Any,
        propagator: Any,
    ):
        if tracer is None:
            import ddtrace

            tracer = ddtrace.tracer  # type: ignore[reportPrivateImportUsage]

            # Cache Context here so start_span never imports it as an extern call.
            # Deferred imports inside externs go through the sandbox's restricted
            # importer and fail with RestrictedWorkflowAccessError.
            from ddtrace._trace.context import (
                Context,  # type: ignore[reportPrivateImportUsage]
            )

            self.ctx_cls: Any = Context
        else:
            self.ctx_cls = None
        self.tracer = tracer
        self.service_name = service_name
        self.on_span_finish = on_span_finish
        self.annotator = annotator
        self.propagator = propagator

    def start_span(
        self,
        *,
        operation_name: str,
        parent_ctx: Any,
        resource_name: str,
        activate: bool,
        start_time: Any = None,
        span_id: int | None = None,
        attributes: Mapping[str, Any] | None = None,
        parent_from_header: bool = False,
        trace_id: int | None = None,
    ) -> Any:
        # No DD header (uninstrumented client) the call passes a deterministic trace_id
        # to keep the RunWorkflow trace consistent if the worker restarts mid-run.
        # Mutating span.trace_id after creation breaks the tracer's internal trace registry.
        effective_parent = parent_ctx
        if trace_id is not None and parent_ctx is None and self.ctx_cls is not None:
            effective_parent = self.ctx_cls(
                trace_id=trace_id, span_id=None, is_remote=True
            )
        span = self.tracer.start_span(
            name=f"{TEMPORAL_TAG_PREFIX}{operation_name}",
            child_of=effective_parent,
            service=self.service_name,
            resource=resource_name,
            activate=activate,
        )
        if start_time is not None:
            span.start_ns = start_time
        if span_id is not None:
            span.span_id = span_id
            if getattr(span, "context", None) is not None:
                span.context.span_id = span_id
        force_keep = parent_ctx is None or parent_from_header
        self.annotator.annotate(
            span,
            operation_name,
            attributes,
            self.propagator.get_baggage(parent_ctx),
            force_keep,
        )
        self.propagator.set_baggage(span.context)
        return span

    def finish_span(
        self,
        span: Any,
        operation_name: str,
        exc: BaseException | None,
    ) -> None:
        try:
            result: FinishResult | None = None
            if self.on_span_finish is not None:
                result = self.on_span_finish(
                    FinishContext(operation=operation_name, exception=exc)
                )

            if exc and not self._should_skip_error(exc):
                span.set_exc_info(type(exc), exc, exc.__traceback__)

            if result is not None and result.extra_tags:
                for key, value in result.extra_tags.items():
                    span.set_tag(key, value)
        finally:
            span.finish()

    def _should_skip_error(self, exc: BaseException | None) -> bool:
        if exc is None:
            return True
        if isinstance(exc, temporalio.workflow.ContinueAsNewError):
            return True
        if isinstance(exc, temporalio.activity._CompleteAsyncError):
            return True
        return False
