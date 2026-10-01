"""Attach OpenTelemetry contexts so that the matching detach is always safe."""

from __future__ import annotations

import threading
from contextvars import Token
from dataclasses import dataclass

import opentelemetry.context
from opentelemetry.context import Context


@dataclass(frozen=True)
class AttachedContext:
    """A context attached by :func:`attach_context`, with what its detach needs."""

    context: Context
    token: Token[Context]
    thread: threading.Thread

    def detach(self) -> None:
        """Detach the context only where the token is valid.

        Generator finalization and GC can run a ``finally`` on a different
        thread or ``contextvars.Context`` than the one that attached, where the
        token is invalid and ``opentelemetry.context.detach`` logs "Failed to
        detach context". Checking that the attached context is still current is
        not enough on its own: OpenTelemetry's threading instrumentation
        (enabled by strands, among others) propagates the same ``Context``
        object into new threads. Requiring the attaching thread as well (the
        ``Thread`` object, so a recycled thread id cannot match) closes that gap.
        """
        if (
            threading.current_thread() is self.thread
            and self.context is opentelemetry.context.get_current()
        ):
            opentelemetry.context.detach(self.token)


def attach_context(context: Context) -> AttachedContext:
    """Attach ``context`` and remember what a safe detach needs."""
    return AttachedContext(
        context, opentelemetry.context.attach(context), threading.current_thread()
    )
