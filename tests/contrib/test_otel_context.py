"""Tests for TemporalAwareContext."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.context.context import Context

from temporalio.contrib.otel_context import TemporalAwareContext


class TestTemporalAwareContext:
    """Tests for TemporalAwareContext behavior.

    Note: Tests for workflow.instance() integration are in integration tests
    since they require an actual workflow context. These unit tests verify
    the contextvars-based behavior.
    """

    def test_attach_and_get_without_workflow(self):
        """Test attach/get_current works outside workflow context."""
        ctx = TemporalAwareContext()
        test_context = Context({"key": "value"})

        token = ctx.attach(test_context)
        assert ctx.get_current() == test_context

        ctx.detach(token)
        # After detach, should return empty context
        assert ctx.get_current() == Context()

    def test_handles_workflow_instance_not_available(self):
        """Test graceful handling when workflow.instance() is not available."""
        ctx = TemporalAwareContext()
        test_context = Context({"key": "value"})

        # The real workflow.instance() will raise when not in workflow
        # This tests that attach/get work via contextvars alone

        # Attach should not raise
        token = ctx.attach(test_context)

        # Get should work via contextvars
        assert ctx.get_current() == test_context

        ctx.detach(token)

    def test_returns_empty_context_when_nothing_available(self):
        """Test returns empty Context when no context is stored anywhere."""
        ctx = TemporalAwareContext()

        # Without attaching anything, and with workflow.instance() failing,
        # should return empty context
        result = ctx.get_current()
        assert result == Context()

    def test_multiple_attach_detach_cycles(self):
        """Test multiple attach/detach cycles work correctly."""
        ctx = TemporalAwareContext()
        context1 = Context({"id": "1"})
        context2 = Context({"id": "2"})

        token1 = ctx.attach(context1)
        assert ctx.get_current() == context1

        token2 = ctx.attach(context2)
        assert ctx.get_current() == context2

        ctx.detach(token2)
        assert ctx.get_current() == context1

        ctx.detach(token1)
        assert ctx.get_current() == Context()

    def test_context_isolation_between_instances(self):
        """Test that each TemporalAwareContext instance has its own storage."""
        ctx1 = TemporalAwareContext()
        ctx2 = TemporalAwareContext()
        context1 = Context({"instance": "1"})
        context2 = Context({"instance": "2"})

        token1 = ctx1.attach(context1)
        token2 = ctx2.attach(context2)

        # Each instance should return its own attached context
        assert ctx1.get_current() == context1
        assert ctx2.get_current() == context2

        ctx1.detach(token1)
        ctx2.detach(token2)

    def test_detach_restores_previous_context(self):
        """Test that detach properly restores the previous context value."""
        ctx = TemporalAwareContext()
        context1 = Context({"level": "1"})
        context2 = Context({"level": "2"})
        context3 = Context({"level": "3"})

        token1 = ctx.attach(context1)
        token2 = ctx.attach(context2)
        token3 = ctx.attach(context3)

        assert ctx.get_current() == context3

        ctx.detach(token3)
        assert ctx.get_current() == context2

        ctx.detach(token2)
        assert ctx.get_current() == context1

        ctx.detach(token1)
        assert ctx.get_current() == Context()

    def test_get_current_returns_context_not_none(self):
        """Test that get_current always returns a Context, never None."""
        ctx = TemporalAwareContext()

        # Without any attached context
        result = ctx.get_current()
        assert result is not None
        assert isinstance(result, Context)
