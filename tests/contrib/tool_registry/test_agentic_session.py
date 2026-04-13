"""Unit tests for AgenticSession and agentic_session.

Tests run without an API key or Temporal server. activity.info() and
activity.heartbeat() are mocked to avoid needing a running worker.
"""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import MagicMock, patch

import pytest

from temporalio.contrib.tool_registry import (
    AgenticSession,
    ToolRegistry,
    agentic_session,
)
from temporalio.contrib.tool_registry.testing import (
    MockAgenticSession,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_activity_info(heartbeat_details=None):
    info = MagicMock()
    info.heartbeat_details = heartbeat_details or []
    return info


# ── agentic_session context manager tests ────────────────────────────────────


async def test_fresh_start_empty_state():
    """No heartbeat details → session starts with empty messages and issues."""
    with patch("temporalio.contrib.tool_registry._session.activity") as mock_activity:
        mock_activity.info.return_value = _make_activity_info()
        async with agentic_session() as session:
            assert session.messages == []
            assert session.issues == []


async def test_restores_from_checkpoint():
    """Heartbeat details present → session restores messages and issues on retry."""
    saved = {
        "messages": [{"role": "user", "content": "analyze this"}],
        "issues": [{"type": "missing", "symbol": "patched", "description": "removed"}],
    }
    with patch("temporalio.contrib.tool_registry._session.activity") as mock_activity:
        mock_activity.info.return_value = _make_activity_info(
            heartbeat_details=[json.dumps(saved)]
        )
        async with agentic_session() as session:
            assert session.messages == saved["messages"]
            assert len(session.issues) == 1
            assert session.issues[0]["type"] == "missing"
            assert session.issues[0]["symbol"] == "patched"


async def test_ignores_invalid_checkpoint():
    """Corrupted heartbeat details → session starts fresh (no crash)."""
    with patch("temporalio.contrib.tool_registry._session.activity") as mock_activity:
        mock_activity.info.return_value = _make_activity_info(
            heartbeat_details=["not valid json{{"]
        )
        async with agentic_session() as session:
            assert session.messages == []
            assert session.issues == []


# ── AgenticSession._checkpoint tests ─────────────────────────────────────────


def test_checkpoint_serializes_messages_and_issues():
    """_checkpoint() heartbeats JSON with messages + issues."""
    heartbeat_calls = []
    with patch("temporalio.contrib.tool_registry._session.activity") as mock_activity:
        mock_activity.heartbeat.side_effect = lambda s: heartbeat_calls.append(
            json.loads(s)
        )

        @dataclasses.dataclass
        class Issue:
            type: str
            symbol: str
            description: str

        session = AgenticSession(
            messages=[{"role": "user", "content": "hi"}],
            issues=[Issue(type="missing", symbol="x", description="gone")],
        )
        session._checkpoint()

    assert len(heartbeat_calls) == 1
    payload = heartbeat_calls[0]
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert len(payload["issues"]) == 1
    assert payload["issues"][0]["type"] == "missing"


def test_checkpoint_empty_state():
    """_checkpoint() with no messages/issues produces valid empty JSON."""
    heartbeat_calls = []
    with patch("temporalio.contrib.tool_registry._session.activity") as mock_activity:
        mock_activity.heartbeat.side_effect = lambda s: heartbeat_calls.append(
            json.loads(s)
        )
        AgenticSession()._checkpoint()

    assert heartbeat_calls[0] == {"version": 1, "messages": [], "issues": []}


def test_checkpoint_plain_dict_issues():
    """_checkpoint() handles plain dict issues (not just dataclasses)."""
    heartbeat_calls = []
    with patch("temporalio.contrib.tool_registry._session.activity") as mock_activity:
        mock_activity.heartbeat.side_effect = lambda s: heartbeat_calls.append(
            json.loads(s)
        )
        session = AgenticSession(
            issues=[
                {"type": "deprecated", "symbol": "old_api", "description": "use new"}
            ]
        )
        session._checkpoint()

    assert heartbeat_calls[0]["issues"][0]["symbol"] == "old_api"


# ── AgenticSession.run_tool_loop tests ────────────────────────────────────────

_ENV = {"ANTHROPIC_API_KEY": "sk-ant-test"}


def _make_mock_anthropic_client(
    responses: list[bool],
    tool_name: str = "noop",
) -> MagicMock:
    """Build a mock Anthropic client.

    Args:
        responses: list of bools — True = done (end_turn, no tools),
            False = not done (returns a tool_use block to continue).
        tool_name: Tool name for tool_use blocks when not done.
    """
    client = MagicMock()
    mock_responses = []
    for done in responses:
        msg = MagicMock()
        if done:
            msg.content = [MagicMock(type="text", text="done")]
            msg.stop_reason = "end_turn"
        else:
            # Return a tool_use block so run_turn continues
            tool_block = MagicMock()
            tool_block.type = "tool_use"
            tool_block.id = "test_id"
            tool_block.name = tool_name
            tool_block.input = {}
            # model_dump needed for _blocks_to_dicts
            tool_block.model_dump.return_value = {
                "type": "tool_use",
                "id": "test_id",
                "name": tool_name,
                "input": {},
            }
            msg.content = [tool_block]
            msg.stop_reason = "tool_use"
        mock_responses.append(msg)
    client.messages.create.side_effect = mock_responses
    return client


async def test_run_tool_loop_adds_prompt_on_fresh_start():
    """On fresh start, run_tool_loop adds the user prompt as the first message."""
    import os

    session = AgenticSession()
    mock_client = _make_mock_anthropic_client([True])  # done on first turn

    with (
        patch("temporalio.contrib.tool_registry._session.activity") as mock_activity,
        patch.dict(os.environ, _ENV),
    ):
        mock_activity.heartbeat = MagicMock()
        await session.run_tool_loop(
            registry=ToolRegistry(),
            provider="anthropic",
            system="system",
            prompt="my prompt",
            client=mock_client,
        )

    assert session.messages[0] == {"role": "user", "content": "my prompt"}


async def test_run_tool_loop_skips_prompt_on_resume():
    """On retry with existing messages, the prompt is not added again."""
    import os

    existing = [
        {"role": "user", "content": "original prompt"},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    session = AgenticSession(messages=list(existing))
    mock_client = _make_mock_anthropic_client([True])  # done on first turn

    with (
        patch("temporalio.contrib.tool_registry._session.activity") as mock_activity,
        patch.dict(os.environ, _ENV),
    ):
        mock_activity.heartbeat = MagicMock()
        await session.run_tool_loop(
            registry=ToolRegistry(),
            provider="anthropic",
            system="system",
            prompt="new prompt that should be ignored",
            client=mock_client,
        )

    # First two messages unchanged
    assert session.messages[:2] == existing


async def test_run_tool_loop_checkpoints_each_turn():
    """_checkpoint is called once per turn before the LLM call."""
    import os

    session = AgenticSession(messages=[{"role": "user", "content": "go"}])
    checkpoint_count = [0]
    # Script 3 turns: first 2 return not-done (tool_use), third returns done
    registry = ToolRegistry()
    registry.handler({"name": "noop", "description": "d", "input_schema": {}})(
        lambda _: "ok"
    )
    mock_client = _make_mock_anthropic_client([False, False, True], tool_name="noop")

    def counting_checkpoint():
        checkpoint_count[0] += 1

    with (
        patch("temporalio.contrib.tool_registry._session.activity") as mock_activity,
        patch.object(session, "_checkpoint", side_effect=counting_checkpoint),
        patch.dict(os.environ, _ENV),
    ):
        mock_activity.heartbeat = MagicMock()
        await session.run_tool_loop(
            registry=registry,
            provider="anthropic",
            system="s",
            prompt="ignored",
            client=mock_client,
        )

    assert checkpoint_count[0] == 3


async def test_run_tool_loop_unknown_provider_raises():
    """Unknown provider raises ValueError."""
    session = AgenticSession(messages=[{"role": "user", "content": "x"}])
    with patch("temporalio.contrib.tool_registry._session.activity") as mock_activity:
        mock_activity.heartbeat = MagicMock()
        with pytest.raises(ValueError, match="Unknown provider"):
            await session.run_tool_loop(
                registry=ToolRegistry(),
                provider="gemini",
                system="s",
                prompt="p",
            )


# ── Checkpoint round-trip test (T6) ──────────────────────────────────────────


def test_checkpoint_round_trip_preserves_tool_calls():
    """Round-trip: checkpoint with tool_calls serializes/deserializes correctly.

    Catches the class of bug where nested dicts lose their type after a
    JSON serialize→deserialize cycle (mirrors the .NET List<object?> bug).
    """
    tool_calls_in_memory = [
        {
            "id": "call_abc",
            "type": "function",
            "function": {"name": "my_tool", "arguments": '{"x": 1}'},
        }
    ]
    assistant_msg = {"role": "assistant", "tool_calls": tool_calls_in_memory}
    issue = {"type": "smell", "file": "foo.py"}

    session = AgenticSession(messages=[assistant_msg], issues=[issue])
    heartbeat_calls: list[str] = []

    with patch("temporalio.contrib.tool_registry._session.activity") as mock_activity:
        mock_activity.heartbeat.side_effect = lambda s: heartbeat_calls.append(s)
        session._checkpoint()

    assert len(heartbeat_calls) == 1
    restored = json.loads(heartbeat_calls[0])

    assert restored["messages"][0]["role"] == "assistant"
    tool_calls_restored = restored["messages"][0]["tool_calls"]
    assert isinstance(tool_calls_restored, list)
    assert len(tool_calls_restored) == 1
    assert tool_calls_restored[0]["id"] == "call_abc"
    assert tool_calls_restored[0]["function"]["name"] == "my_tool"
    assert restored["issues"][0]["type"] == "smell"
    assert restored["issues"][0]["file"] == "foo.py"


# ── heartbeat_every tests (T7) ────────────────────────────────────────────────


async def test_heartbeat_every_default_checkpoints_each_turn():
    """heartbeat_every=1 (default) checkpoints before every turn."""
    import os

    session = AgenticSession(messages=[{"role": "user", "content": "go"}])
    registry = ToolRegistry()
    registry.handler({"name": "noop", "description": "d", "input_schema": {}})(
        lambda _: "ok"
    )
    mock_client = _make_mock_anthropic_client([False, False, True], tool_name="noop")
    checkpoint_count = [0]

    def counting_checkpoint():
        checkpoint_count[0] += 1

    with (
        patch("temporalio.contrib.tool_registry._session.activity") as mock_activity,
        patch.object(session, "_checkpoint", side_effect=counting_checkpoint),
        patch.dict(os.environ, _ENV),
    ):
        mock_activity.heartbeat = MagicMock()
        await session.run_tool_loop(
            registry=registry,
            provider="anthropic",
            system="s",
            prompt="ignored",
            heartbeat_every=1,
            client=mock_client,
        )

    assert checkpoint_count[0] == 3  # one checkpoint per turn


async def test_heartbeat_every_n_skips_turns():
    """heartbeat_every=3 checkpoints on turns 1, 4, 7, ..."""
    import os

    session = AgenticSession(messages=[{"role": "user", "content": "go"}])
    registry = ToolRegistry()
    registry.handler({"name": "noop", "description": "d", "input_schema": {}})(
        lambda _: "ok"
    )
    # 4 turns: [tool, tool, tool, done]
    mock_client = _make_mock_anthropic_client(
        [False, False, False, True], tool_name="noop"
    )
    checkpoint_count = [0]

    def counting_checkpoint():
        checkpoint_count[0] += 1

    with (
        patch("temporalio.contrib.tool_registry._session.activity") as mock_activity,
        patch.object(session, "_checkpoint", side_effect=counting_checkpoint),
        patch.dict(os.environ, _ENV),
    ):
        mock_activity.heartbeat = MagicMock()
        await session.run_tool_loop(
            registry=registry,
            provider="anthropic",
            system="s",
            prompt="ignored",
            heartbeat_every=3,
            client=mock_client,
        )

    # 4 turns, heartbeat_every=3 → checkpoints on turns 1 and 4 → 2 checkpoints
    assert checkpoint_count[0] == 2


# ── MockAgenticSession tests ──────────────────────────────────────────────────


async def test_mock_agentic_session_returns_pre_canned_issues():
    """MockAgenticSession returns fixed issues without LLM calls."""
    session = MockAgenticSession(
        issues=[{"type": "deprecated", "symbol": "old_fn", "description": "removed"}]
    )
    await session.run_tool_loop(
        registry=ToolRegistry(),
        provider="anthropic",
        system="s",
        prompt="p",
    )
    assert len(session.issues) == 1
    assert session.issues[0]["symbol"] == "old_fn"


async def test_mock_agentic_session_empty_issues():
    """MockAgenticSession with no issues starts empty."""
    session = MockAgenticSession()
    assert session.issues == []


# ── Integration test (skipped unless RUN_INTEGRATION_TESTS=true) ─────────────


@pytest.mark.skipif(
    not __import__("os").environ.get("RUN_INTEGRATION_TESTS"),
    reason="RUN_INTEGRATION_TESTS not set",
)
async def test_crash_resume():
    """Integration: activity crashes mid-loop; second attempt resumes from checkpoint.

    Uses WorkflowEnvironment to run a real Temporal worker.  The first activity
    attempt crashes after 2 turns; the second attempt should restore from the
    turn-2 checkpoint and complete from there, not from turn 0.
    """
    # This test requires a running Temporal server (temporal server start-dev)
    # and would use WorkflowEnvironment.start_local() to spin up a test server.
    # Omitted here to keep the test file self-contained; see the project README
    # for instructions on running the full integration suite.
    pytest.skip("Full integration test requires WorkflowEnvironment setup — see README")
