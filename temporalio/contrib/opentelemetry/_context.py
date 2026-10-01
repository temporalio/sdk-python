"""Attach OpenTelemetry contexts so that the matching detach is always safe."""

from __future__ import annotations

from contextvars import Token
from dataclasses import dataclass

import opentelemetry.context
from opentelemetry.context import Context


@dataclass(frozen=True)
class AttachedContext:
    """A context attached by :func:`attach_context`, with what its detach needs."""

    context: Context
    token: Token[Context]

    def detach(self) -> bool:
        """Detach the context where its token is valid; return whether it was.

        The attach and the detach of one interceptor call can run on different
        threads: workflow activations run on a thread pool while the asyncio
        task keeps its ``contextvars.Context`` across them, so the token is
        still valid there and the detach must happen. Generator finalization
        and GC, on the other hand, can run a ``finally`` in a different
        ``contextvars.Context``, where the token is invalid and
        ``opentelemetry.context.detach`` logs "Failed to detach context".
        Checking that the attached context is still current cannot tell those
        apart, because OpenTelemetry's threading instrumentation (enabled by
        strands, among others) propagates the same ``Context`` object into new
        threads. Only ``contextvars`` knows which ``Context`` a token belongs
        to, so this performs the reset that ``opentelemetry.context.detach``
        performs and treats its ``ValueError`` for a foreign ``Context`` as
        "nothing to detach here".
        """
        if self.context is not opentelemetry.context.get_current():
            return False
        try:
            self.token.var.reset(self.token)
        except ValueError:
            # The token was created in a different contextvars.Context.
            return False
        return True


def attach_context(context: Context) -> AttachedContext:
    """Attach ``context`` and remember what a safe detach needs."""
    return AttachedContext(context, opentelemetry.context.attach(context))
