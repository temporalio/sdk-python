"""Unit tests for ToolRegistry.

Tests run without an API key or Temporal server.  LLM calls are replaced by
:class:`MockProvider` from ``testing.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from temporalio.contrib.tool_registry import ToolRegistry, run_tool_loop
from temporalio.contrib.tool_registry.testing import (
    CrashAfterTurns,
    FakeToolRegistry,
    MockProvider,
    ResponseBuilder,
)

# ── ToolRegistry unit tests ───────────────────────────────────────────────────


def test_dispatch_calls_handler():
    registry = ToolRegistry()

    @registry.handler({"name": "greet", "description": "d", "input_schema": {}})
    def handle_greet(inp: dict) -> str:
        return f"hello {inp.get('name')}"

    assert registry.dispatch("greet", {"name": "world"}) == "hello world"


def test_dispatch_unknown_raises():
    registry = ToolRegistry()
    with pytest.raises(KeyError, match="unknown"):
        registry.dispatch("unknown", {})


def test_adispatch_sync_handler():
    """adispatch works with a plain def handler."""
    registry = ToolRegistry()

    @registry.handler({"name": "greet", "description": "d", "input_schema": {}})
    def handle_greet(inp: dict) -> str:
        return f"hello {inp.get('name')}"

    result = asyncio.run(registry.adispatch("greet", {"name": "world"}))
    assert result == "hello world"


def test_adispatch_async_handler():
    """adispatch awaits async handlers."""
    registry = ToolRegistry()

    @registry.handler({"name": "async_greet", "description": "d", "input_schema": {}})
    async def handle_async_greet(inp: dict) -> str:
        return f"async hello {inp.get('name')}"

    result = asyncio.run(registry.adispatch("async_greet", {"name": "world"}))
    assert result == "async hello world"


def test_dispatch_async_handler_raises_typeerror():
    """dispatch() on an async handler raises TypeError — use adispatch instead."""
    registry = ToolRegistry()

    @registry.handler({"name": "async_tool", "description": "d", "input_schema": {}})
    async def handle(inp: dict) -> str:
        return "async result"

    with pytest.raises(TypeError, match="adispatch"):
        registry.dispatch("async_tool", {})


def test_to_openai_format():
    registry = ToolRegistry()

    @registry.handler(
        {
            "name": "my_tool",
            "description": "Does something useful.",
            "input_schema": {
                "type": "object",
                "properties": {"arg": {"type": "string"}},
                "required": ["arg"],
            },
        }
    )
    def handle(inp: dict) -> str:
        return "ok"

    result = registry.to_openai()
    assert len(result) == 1
    converted = result[0]
    assert converted["type"] == "function"
    assert converted["function"]["name"] == "my_tool"
    assert converted["function"]["description"] == "Does something useful."
    assert "arg" in converted["function"]["parameters"]["properties"]


def test_to_anthropic_returns_definitions_unchanged():
    defn = {"name": "t", "description": "d", "input_schema": {}}
    registry = ToolRegistry()
    registry.handler(defn)(lambda inp: "ok")
    assert registry.to_anthropic() == [defn]


def test_multiple_tools():
    registry = ToolRegistry()
    registry.handler({"name": "alpha", "description": "a", "input_schema": {}})(
        lambda _: "a"
    )
    registry.handler({"name": "beta", "description": "b", "input_schema": {}})(
        lambda _: "b"
    )
    result = registry.to_openai()
    assert len(result) == 2
    assert result[0]["function"]["name"] == "alpha"
    assert result[1]["function"]["name"] == "beta"


def test_from_mcp_tools():
    """from_mcp_tools wraps MCP-style objects into definitions."""

    class FakeMCPTool:
        def __init__(self, name: str, description: str, schema: dict):
            self.name = name
            self.description = description
            self.inputSchema = schema

    mcp_tools = [
        FakeMCPTool("search", "Search files", {"type": "object", "properties": {}}),
        FakeMCPTool("read", "Read a file", {"type": "object", "properties": {}}),
    ]

    registry = ToolRegistry.from_mcp_tools(mcp_tools)
    assert len(registry.to_anthropic()) == 2
    names = {d["name"] for d in registry.to_anthropic()}
    assert names == {"search", "read"}


# ── MockProvider tests ────────────────────────────────────────────────────────


def test_mock_provider_dispatches_tool_calls():
    """MockProvider dispatches tool calls and runs the loop to completion."""
    collected: list[str] = []
    registry = ToolRegistry()

    @registry.handler({"name": "collect", "description": "d", "input_schema": {}})
    def handle_collect(inp: dict) -> str:
        collected.append(inp.get("value", ""))
        return "ok"

    provider = MockProvider(
        [
            ResponseBuilder.tool_call("collect", {"value": "first"}),
            ResponseBuilder.tool_call("collect", {"value": "second"}),
            ResponseBuilder.done("all done"),
        ]
    )
    messages: list[dict] = [{"role": "user", "content": "go"}]
    provider.run_loop(messages, registry)

    assert collected == ["first", "second"]


def test_mock_provider_stops_on_done():
    provider = MockProvider([ResponseBuilder.done("finished")])
    messages: list[dict] = [{"role": "user", "content": "x"}]
    provider.run_loop(messages)
    # One user message + one assistant message
    assert len(messages) == 2
    assert messages[-1]["role"] == "assistant"


def test_mock_provider_stops_when_exhausted():
    """If responses are exhausted, run_loop stops cleanly."""
    provider = MockProvider([])
    messages: list[dict] = [{"role": "user", "content": "x"}]
    provider.run_loop(messages)
    assert len(messages) == 1  # nothing added


# ── FakeToolRegistry tests ────────────────────────────────────────────────────


def test_fake_registry_records_calls():
    fake = FakeToolRegistry()

    @fake.handler({"name": "greet", "description": "d", "input_schema": {}})
    def handle(inp: dict) -> str:
        return "ok"

    fake.dispatch("greet", {"name": "world"})
    fake.dispatch("greet", {"name": "temporal"})

    assert fake.calls == [("greet", {"name": "world"}), ("greet", {"name": "temporal"})]


# ── run_tool_loop tests ───────────────────────────────────────────────────────


def test_run_tool_loop_unknown_provider_raises():
    async def _run():
        await run_tool_loop(
            provider="gemini",
            system="s",
            prompt="p",
            tools=ToolRegistry(),
        )

    with pytest.raises(ValueError, match="gemini"):
        asyncio.run(_run())


# ── CrashAfterTurns tests ─────────────────────────────────────────────────────


def test_crash_after_turns_raises():
    crasher = CrashAfterTurns(1)
    messages: list[dict] = [{"role": "user", "content": "x"}]
    # First turn: fine
    crasher.run_turn(messages, ToolRegistry())
    # Second turn: crashes
    with pytest.raises(RuntimeError, match="simulated crash"):
        crasher.run_turn(messages, ToolRegistry())


# ── is_error / handler error tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_handler_error_sets_is_error_and_does_not_crash():
    """Handler exceptions are caught; the tool result carries is_error=True."""
    from temporalio.contrib.tool_registry._providers import AnthropicProvider

    registry = ToolRegistry()

    @registry.handler({"name": "boom", "description": "d", "input_schema": {}})
    def handle(inp: dict) -> str:
        raise ValueError("intentional failure")

    # Minimal Anthropic mock: first call returns a tool_use, second returns end_turn.
    calls: list[int] = []

    class _MockMessages:
        def create(self, **_kwargs):  # type: ignore[override]
            calls.append(1)
            if len(calls) == 1:
                return _FakeResponse(
                    content=[{"type": "tool_use", "id": "c1", "name": "boom", "input": {}}],
                    stop_reason="tool_use",
                )
            return _FakeResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    class _FakeClient:
        messages = _MockMessages()

    class _FakeResponse:
        def __init__(self, content, stop_reason):
            self.content = content
            self.stop_reason = stop_reason

    provider = AnthropicProvider(registry, "sys", client=_FakeClient())
    messages: list[dict] = [{"role": "user", "content": "go"}]
    await provider.run_turn(messages)

    # messages[1] is the assistant message; messages[2] is the tool result wrapper.
    tool_result_msg = messages[2]
    assert tool_result_msg["role"] == "user"
    tool_result = tool_result_msg["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["is_error"] is True
    assert "intentional failure" in tool_result["content"]


@pytest.mark.asyncio
async def test_async_handler_invoked_via_adispatch():
    """Async handlers are awaited by providers via adispatch."""
    from temporalio.contrib.tool_registry._providers import AnthropicProvider

    registry = ToolRegistry()
    invocations: list[str] = []

    @registry.handler({"name": "async_tool", "description": "d", "input_schema": {}})
    async def handle(inp: dict) -> str:
        invocations.append("called")
        return "async result"

    calls: list[int] = []

    class _MockMessages:
        def create(self, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                return _FakeResponse(
                    content=[{"type": "tool_use", "id": "c1", "name": "async_tool", "input": {}}],
                    stop_reason="tool_use",
                )
            return _FakeResponse(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")

    class _FakeClient:
        messages = _MockMessages()

    class _FakeResponse:
        def __init__(self, content, stop_reason):
            self.content = content
            self.stop_reason = stop_reason

    provider = AnthropicProvider(registry, "sys", client=_FakeClient())
    messages: list[dict] = [{"role": "user", "content": "go"}]
    await provider.run_turn(messages)

    assert invocations == ["called"]
    # messages[1] is the assistant message; messages[2] is the tool result wrapper.
    tool_result_msg = messages[2]
    tool_result = tool_result_msg["content"][0]
    assert tool_result["content"] == "async result"
    assert "is_error" not in tool_result


@pytest.mark.asyncio
async def test_openai_handler_error_does_not_crash():
    """Handler exceptions in OpenAI provider are caught and returned as error strings."""
    from temporalio.contrib.tool_registry._providers import OpenAIProvider

    registry = ToolRegistry()

    @registry.handler({"name": "boom", "description": "d", "input_schema": {}})
    def handle(inp: dict) -> str:
        raise RuntimeError("openai error test")

    class _MockCompletions:
        def create(self, **_kwargs):  # type: ignore[override]
            return _FakeResp()

    class _FakeClient:
        class chat:
            completions = _MockCompletions()

    class _FakeTc:
        id = "tc1"
        type = "function"

        class function:
            name = "boom"
            arguments = "{}"

    class _FakeMsg:
        content = None
        tool_calls = [_FakeTc()]

    class _FakeChoice:
        message = _FakeMsg()
        finish_reason = "tool_calls"

    class _FakeResp:
        choices = [_FakeChoice()]

    provider = OpenAIProvider(registry, "sys", client=_FakeClient())
    messages: list[dict] = [{"role": "user", "content": "go"}]
    # Should not raise even though the handler throws.
    try:
        await provider.run_turn(messages)
    except Exception as e:
        pytest.fail(f"run_turn raised unexpectedly: {e}")

    tool_msg = messages[-1]
    assert tool_msg["role"] == "tool"
    assert "openai error test" in tool_msg["content"]


# ── Integration test (skipped unless RUN_INTEGRATION_TESTS=true) ─────────────


@pytest.mark.skipif(
    not __import__("os").environ.get("RUN_INTEGRATION_TESTS"),
    reason="RUN_INTEGRATION_TESTS not set",
)
@pytest.mark.asyncio
async def test_integration_anthropic_real_call():
    """End-to-end: run_tool_loop with real Anthropic API call."""
    import os

    assert os.environ.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY required"

    collected: list[str] = []
    tools = ToolRegistry()

    @tools.handler(
        {
            "name": "record",
            "description": "Record a value",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        }
    )
    def handle_record(inp: dict) -> str:
        collected.append(inp["value"])
        return "recorded"

    await run_tool_loop(
        provider="anthropic",
        system="You must call record() exactly once with value='hello'.",
        prompt="Please call the record tool with value='hello'.",
        tools=tools,
    )

    assert "hello" in collected


@pytest.mark.skipif(
    not __import__("os").environ.get("RUN_INTEGRATION_TESTS"),
    reason="RUN_INTEGRATION_TESTS not set",
)
@pytest.mark.asyncio
async def test_integration_openai_real_call():
    """End-to-end: run_tool_loop with real OpenAI API call."""
    import os

    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY required"

    collected: list[str] = []
    tools = ToolRegistry()

    @tools.handler(
        {
            "name": "record",
            "description": "Record a value",
            "input_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        }
    )
    def handle_record(inp: dict) -> str:
        collected.append(inp["value"])
        return "recorded"

    await run_tool_loop(
        provider="openai",
        system="You must call record() exactly once with value='hello'.",
        prompt="Please call the record tool with value='hello'.",
        tools=tools,
    )

    assert "hello" in collected
