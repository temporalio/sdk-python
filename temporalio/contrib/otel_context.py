"""OpenTelemetry context implementation for Temporal workflow sandboxes.

The Temporal workflow sandbox isolates Python's contextvars, which breaks
OpenTelemetry's default context propagation. This module provides a custom
OTEL context implementation that stores context on both contextvars (for
normal use) AND workflow.instance() (for sandbox access).

Usage:
    # Option 1: Set environment variable before any OTEL imports
    import os
    os.environ["OTEL_PYTHON_CONTEXT"] = "temporal_aware_context"

    # Option 2: Import this module before OTEL to auto-register
    # (requires entry point registration in pyproject.toml)

Then use standard TracingInterceptor - context propagation just works:
    from temporalio.contrib.opentelemetry import TracingInterceptor
    interceptor = TracingInterceptor()
    client = await Client.connect("localhost:7233", interceptors=[interceptor])
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

from opentelemetry.context.context import Context, _RuntimeContext

if TYPE_CHECKING:
    pass

# Attribute name for storing context on workflow instance
_WORKFLOW_CONTEXT_ATTR = "__temporal_otel_context"


class TemporalAwareContext(_RuntimeContext):
    """OTEL context implementation that survives Temporal sandbox isolation.

    The Temporal workflow sandbox isolates contextvars, which breaks OTEL's
    default context propagation. This implementation stores context on both
    contextvars (for normal use) AND workflow.instance() (for sandbox access).

    How it works:
    - attach(): Stores context in contextvars AND on workflow.instance() if available
    - get_current(): Tries contextvars first, falls back to workflow.instance()
    - detach(): Resets contextvars (workflow storage is overwritten on next attach)

    This makes get_current_span() work transparently inside workflow sandboxes,
    allowing OpenTelemetry instrumentation (like OpenInference) to properly
    parent spans without any special handling.
    """

    def __init__(self) -> None:
        """Initialize the context storage."""
        self._contextvar: ContextVar[Context] = ContextVar(
            "temporal_otel_context", default=Context()
        )

    def attach(self, context: Context) -> Token[Context]:
        """Attach context, storing in both contextvars and workflow instance.

        Args:
            context: The OTEL context to attach.

        Returns:
            Token for detaching/resetting the context.
        """
        # Always store in contextvars (works outside sandbox)
        token = self._contextvar.set(context)

        # Also store on workflow instance if available (works inside sandbox)
        try:
            from temporalio import workflow

            instance = workflow.instance()
            setattr(instance, _WORKFLOW_CONTEXT_ATTR, context)
        except Exception:
            pass  # Not in workflow context, that's fine

        return token

    def get_current(self) -> Context:
        """Get current context, with fallback to workflow instance storage.

        Returns:
            The current OTEL context. Falls back to workflow.instance()
            storage if contextvars returns empty (i.e., inside sandbox).
        """
        # Try contextvars first (works outside sandbox)
        ctx = self._contextvar.get()
        if ctx:
            return ctx

        # Fall back to workflow.instance() (works inside sandbox)
        try:
            from temporalio import workflow

            instance = workflow.instance()
            stored_ctx = getattr(instance, _WORKFLOW_CONTEXT_ATTR, None)
            if stored_ctx is not None:
                return stored_ctx
        except Exception:
            pass  # Not in workflow context

        return Context()

    def detach(self, token: Token[Context]) -> None:
        """Detach context by resetting to previous value.

        Args:
            token: The token returned from attach().
        """
        self._contextvar.reset(token)
        # Note: workflow instance storage is overwritten on next attach,
        # so we don't need to clean it up here
