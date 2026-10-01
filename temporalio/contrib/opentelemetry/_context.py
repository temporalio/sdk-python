"""Attach OpenTelemetry contexts so that the matching detach is always safe."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import Token

import opentelemetry.context
from opentelemetry.context import Context


@contextmanager
def attached_context(context: Context | None) -> Iterator[None]:
    """Attach ``context`` for the block and detach it afterwards where possible.

    ``None`` attaches nothing. The block's ``finally`` can run in a different
    ``contextvars.Context`` than the one that attached: a context manager
    abandoned by an evicted workflow is finalized wherever garbage collection
    happens to run. The token is not valid there, and
    ``opentelemetry.context.detach`` would log "Failed to detach context" even
    though there is nothing to detach. Checking that the attached context is
    still current does not catch every such case, because OpenTelemetry's
    threading instrumentation (enabled by strands, among others) propagates
    the same ``Context`` object into new threads. The thread is no test
    either: workflow activations move between pool threads while the asyncio
    task keeps its ``contextvars.Context``, and those detaches must happen.
    Only ``contextvars`` knows which ``Context`` a token belongs to, so
    :func:`_detach` performs the reset ``detach`` performs and ignores the
    ``ValueError`` raised for a token from another ``Context``.
    """
    if context is None:
        yield
        return
    token = opentelemetry.context.attach(context)
    try:
        yield
    finally:
        _detach(context, token)


def _detach(context: Context, token: Token[Context]) -> bool:
    """Detach ``context`` if it is current and ``token`` is valid here."""
    if context is not opentelemetry.context.get_current():
        return False
    try:
        token.var.reset(token)
    except ValueError:
        # The token was created in a different contextvars.Context.
        return False
    return True
