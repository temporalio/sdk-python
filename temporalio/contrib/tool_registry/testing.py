"""Testing utilities for :mod:`temporalio.contrib.tool_registry`.

Provides mock objects that allow unit tests to exercise ToolRegistry,
AgenticSession, and run_tool_loop without an API key or a running Temporal
server.  Also provides :class:`MockAgenticSession` for testing code that
uses :func:`agentic_session` without any LLM calls.

Example::

    from temporalio.contrib.tool_registry.testing import MockProvider, ResponseBuilder

    # Script two turns: first turn uses a tool, second turn is done.
    provider = MockProvider([
        ResponseBuilder.tool_call("flag_issue", {"description": "wrong"}),
        ResponseBuilder.done("Analysis complete."),
    ])

    messages = [{"role": "user", "content": "analyze this"}]
    provider.run_loop(messages)
    assert len(messages) == 5  # user + assistant + tool_result + assistant + ...
"""

from __future__ import annotations

import uuid
from typing import Any

from temporalio.contrib.tool_registry._registry import ToolRegistry


class ResponseBuilder:
    """Factories for scripting mock LLM turn sequences."""

    @staticmethod
    def tool_call(
        tool_name: str,
        tool_input: dict[str, Any],
        call_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a mock assistant message that makes a single tool call.

        Args:
            tool_name: Name of the tool to call.
            tool_input: Input dict passed to the tool.
            call_id: Optional tool-use ID.  Auto-generated if not provided.

        Returns:
            A dict in Anthropic-style assistant content format.
        """
        cid = call_id or f"test_{uuid.uuid4().hex[:8]}"
        return {
            "_mock_stop": False,
            "content": [
                {"type": "tool_use", "id": cid, "name": tool_name, "input": tool_input}
            ],
        }

    @staticmethod
    def done(text: str = "Done.") -> dict[str, Any]:
        """Create a mock assistant message that ends the loop.

        Args:
            text: Assistant text response.

        Returns:
            A dict indicating the loop should stop.
        """
        return {
            "_mock_stop": True,
            "content": [{"type": "text", "text": text}],
        }


class MockProvider:
    """LLM provider that returns pre-scripted responses without API calls.

    Responses are consumed in order.  Once exhausted, the loop stops.

    Args:
        responses: Sequence of response dicts produced by :class:`ResponseBuilder`.

    Example::

        provider = MockProvider([
            ResponseBuilder.tool_call("greet", {"name": "world"}),
            ResponseBuilder.done("said hello"),
        ])
        messages = [{"role": "user", "content": "greet world"}]
        provider.run_loop(messages, registry)
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        """Initialize MockProvider with a list of scripted responses."""
        self._responses = list(responses)
        self._index = 0

    def run_turn(self, messages: list[dict[str, Any]], registry: ToolRegistry) -> bool:
        """Execute one scripted turn.

        Returns:
            ``True`` when done, ``False`` to continue.
        """
        if self._index >= len(self._responses):
            return True

        response = self._responses[self._index]
        self._index += 1
        stop = response.get("_mock_stop", True)
        content = response.get("content", [])

        messages.append({"role": "assistant", "content": content})

        if not stop:
            tool_results = []
            for block in content:
                if block.get("type") == "tool_use":
                    result = registry.dispatch(block["name"], block.get("input", {}))
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": result,
                        }
                    )
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

        return stop

    def run_loop(
        self,
        messages: list[dict[str, Any]],
        registry: ToolRegistry | None = None,
    ) -> None:
        """Run all scripted turns until exhausted or a done response is reached."""
        if registry is None:
            registry = ToolRegistry()
        while not self.run_turn(messages, registry):
            pass


class FakeToolRegistry(ToolRegistry):
    """A :class:`ToolRegistry` that records all dispatch calls.

    Useful for asserting which tools were called and with what inputs during a
    test run.

    Example::

        fake = FakeToolRegistry()
        fake.handler({"name": "greet", "description": "d", "input_schema": {}})(
            lambda inp: "ok"
        )
        fake.dispatch("greet", {"name": "world"})
        assert fake.calls == [("greet", {"name": "world"})]
    """

    def __init__(self) -> None:
        """Initialize FakeToolRegistry with an empty calls list."""
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def dispatch(self, name: str, input_dict: dict[str, Any]) -> str:
        """Record the call then delegate to the registered handler."""
        self.calls.append((name, input_dict))
        return super().dispatch(name, input_dict)


class MockAgenticSession:
    """A pre-canned :class:`AgenticSession` that returns fixed results without LLM calls.

    Use this to test code that calls :func:`agentic_session` and inspects
    ``session.results`` without needing an API key or a running server.

    Args:
        results: Pre-canned results to populate ``session.results`` immediately
            on construction.

    Example::

        from temporalio.contrib.tool_registry.testing import MockAgenticSession
        from contextlib import asynccontextmanager

        async def run_with_mock(prompt: str) -> list:
            session = MockAgenticSession([{"type": "missing", "symbol": "x", "description": "gone"}])
            # Simulate the agentic loop completing without LLM calls
            return session.results
    """

    def __init__(self, results: list[Any] | None = None) -> None:
        """Initialize MockAgenticSession with optional pre-seeded results."""
        self.messages: list[dict[str, Any]] = []
        self.results: list[Any] = list(results or [])

    async def run_tool_loop(
        self,
        registry: "ToolRegistry",
        provider: str,
        system: str,
        prompt: str,
        model: str | None = None,
        client: Any = None,
    ) -> None:
        """No-op — does not call any LLM."""
        if not self.messages:
            self.messages = [{"role": "user", "content": prompt}]
        # No LLM calls; session.results already set by constructor.

    def _checkpoint(self) -> None:
        """No-op — does not call activity.heartbeat() in tests."""


class CrashAfterTurns:
    """Simulates an activity crash after ``n`` turns.

    Use this in integration tests to verify that :class:`AgenticSession`
    correctly resumes from a checkpoint after a crash.

    Args:
        n: Number of turns to complete before raising :exc:`RuntimeError`.

    Example::

        # First invocation crashes after turn 2.
        # Second invocation (retry) should resume from turn 2's checkpoint.
        crasher = CrashAfterTurns(2)
    """

    def __init__(self, n: int) -> None:
        """Initialize CrashAfterTurns to simulate a crash after n turns."""
        self._n = n
        self._count = 0

    def run_turn(self, messages: list[dict[str, Any]], registry: ToolRegistry) -> bool:
        """Execute one turn, raising RuntimeError after n turns are complete.

        Args:
            messages: Conversation message history, mutated in-place.
            registry: Tool registry (unused — crash-only provider).

        Returns:
            ``True`` when the last allowed turn completes, never returns False.
        """
        self._count += 1
        if self._count > self._n:
            raise RuntimeError(
                f"CrashAfterTurns: simulated crash after {self._n} turns"
            )
        # Produce a no-op done response
        messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": "..."}]}
        )
        return self._count >= self._n

    def run_loop(
        self,
        messages: list[dict[str, Any]],
        registry: ToolRegistry | None = None,
    ) -> None:
        """Run turns until n turns complete or a crash is triggered.

        Args:
            messages: Conversation message history, mutated in-place.
            registry: Optional tool registry; a default empty one is used if None.
        """
        if registry is None:
            registry = ToolRegistry()
        while not self.run_turn(messages, registry):
            pass
