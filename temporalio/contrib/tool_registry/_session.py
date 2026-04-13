"""agentic_session — durable multi-turn LLM activity with heartbeat checkpointing.

Provides :func:`agentic_session`, an async context manager that saves
conversation state via :func:`temporalio.activity.heartbeat` after each
LLM turn. On activity retry, the session is automatically restored from
the last checkpoint so the conversation resumes mid-turn rather than
restarting from the beginning.

This builds on standard Temporal APIs — :func:`activity.heartbeat` and
:attr:`activity.info().heartbeat_details` — and adds the conversation-specific
serialization and restore logic as a reusable primitive.

Example::

    from temporalio.contrib.tool_registry import ToolRegistry, agentic_session

    @activity.defn
    async def analyze(prompt: str) -> list[dict]:
        async with agentic_session() as session:
            tools = ToolRegistry()

            @tools.handler({"name": "flag", "description": "...",
                            "input_schema": {"type": "object",
                                             "properties": {"msg": {"type": "string"}}}})
            def handle_flag(inp: dict) -> str:
                session.issues.append(inp)
                return "recorded"

            await session.run_tool_loop(
                registry=tools,
                provider="anthropic",
                system="You are a code reviewer...",
                prompt=prompt,
            )
        return session.issues
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

_logger = logging.getLogger(__name__)

from temporalio.contrib.tool_registry._registry import ToolRegistry


@dataclasses.dataclass
class AgenticSession:
    """Holds conversation state across a multi-turn LLM tool-use loop.

    Instances are created by :func:`agentic_session` and should not normally
    be constructed directly.  On activity retry, :func:`agentic_session`
    deserializes the saved state from :attr:`activity.info().heartbeat_details`
    and passes it to the constructor.

    Attributes:
        messages: Full conversation history in provider-neutral format.
            Appended to by :meth:`run_tool_loop` on each turn.
        issues: Application-level results accumulated during the session.
            Serialized to JSON for checkpoint storage — elements must be
            JSON-serializable (plain dicts or dataclass instances).
    """

    messages: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    issues: list[Any] = dataclasses.field(default_factory=list)

    async def run_tool_loop(
        self,
        registry: ToolRegistry,
        provider: str,
        system: str,
        prompt: str,
        heartbeat_every: int = 1,
        model: str | None = None,
        client: Any = None,
    ) -> None:
        """Run the agentic tool-use loop to completion.

        If :attr:`messages` is empty (fresh start), adds ``prompt`` as the
        first user message. Otherwise resumes from the existing conversation
        state (retry case).

        Checkpoints via :meth:`_checkpoint` before each LLM call.  If the
        activity is cancelled due to a heartbeat timeout, the next attempt
        will restore from the last checkpoint and continue from there.

        Args:
            registry: Tool registry with handlers for all tools the model
                may call.
            provider: LLM provider — ``"anthropic"`` or ``"openai"``.
            system: System prompt.
            prompt: Initial user message.  Ignored on resume (messages already set).
            heartbeat_every: Heartbeat every N turns (default 1 = every turn).
                Increase to reduce heartbeat overhead for cheap/fast models.
                A crash between heartbeats replays from the previous checkpoint.
            model: Model name override.  If ``None``, the provider default
                is used (``claude-sonnet-4-6`` / ``gpt-4o``).
            client: Pre-constructed LLM client.  Useful in tests to avoid
                API key requirements.

        Raises:
            ValueError: If ``provider`` is not ``"anthropic"`` or ``"openai"``.
        """
        from temporalio.contrib.tool_registry._providers import (
            AnthropicProvider,
            OpenAIProvider,
        )

        if not self.messages:
            self.messages = [{"role": "user", "content": prompt}]

        kwargs: dict[str, Any] = {}
        if model is not None:
            kwargs["model"] = model
        if client is not None:
            kwargs["client"] = client

        if provider == "anthropic":
            if client is None:
                import anthropic as _anthropic

                kwargs["client"] = _anthropic.Anthropic(
                    api_key=os.environ["ANTHROPIC_API_KEY"]
                )
            p = AnthropicProvider(registry, system, **kwargs)
            turn = 0
            while True:
                turn += 1
                if (turn - 1) % heartbeat_every == 0:
                    self._checkpoint()
                if p.run_turn(self.messages):
                    break

        elif provider == "openai":
            if client is None:
                import openai as _openai

                kwargs["client"] = _openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            p_oa = OpenAIProvider(registry, system, **kwargs)
            turn = 0
            while True:
                turn += 1
                if (turn - 1) % heartbeat_every == 0:
                    self._checkpoint()
                if p_oa.run_turn(self.messages):
                    break

        else:
            raise ValueError(
                f"Unknown provider {provider!r}. Use 'anthropic' or 'openai'."
            )

    def _checkpoint(self) -> None:
        """Heartbeat the serialized conversation state for crash-safe resume.

        :func:`activity.heartbeat` sends the payload to the Temporal server.
        On retry, ``activity.info().heartbeat_details[0]`` contains this JSON.
        :func:`agentic_session` reads it on entry and restores messages +
        issues.

        Issues are serialized with :func:`dataclasses.asdict` if they are
        dataclass instances, or left as-is if they are already plain dicts.

        Raises:
            ApplicationError: (non-retryable) If any issue is not JSON-serializable.
        """

        def _serialize_issue(issue: Any, idx: int) -> dict[str, Any]:
            if dataclasses.is_dataclass(issue) and not isinstance(issue, type):
                s = dataclasses.asdict(issue)
            else:
                try:
                    s = dict(issue)
                except (TypeError, ValueError) as e:
                    raise ApplicationError(
                        f"AgenticSession: issues[{idx}] cannot be converted to dict: {e}. "
                        "Store only plain dicts or dataclass instances.",
                        non_retryable=True,
                    ) from e
            try:
                json.dumps(s)
            except (TypeError, ValueError) as e:
                raise ApplicationError(
                    f"AgenticSession: issues[{idx}] is not JSON-serializable: {e}. "
                    "Store only plain dicts or dataclasses with JSON-serializable fields.",
                    non_retryable=True,
                ) from e
            return s

        serialized_issues = [_serialize_issue(iss, i) for i, iss in enumerate(self.issues)]

        activity.heartbeat(
            json.dumps(
                {
                    "version": 1,
                    "messages": self.messages,
                    "issues": serialized_issues,
                }
            )
        )


@contextlib.asynccontextmanager
async def agentic_session() -> AsyncGenerator[AgenticSession, None]:
    """Async context manager for a durable, checkpointed LLM tool-use session.

    On entry, restores conversation state (messages + issues) from
    :attr:`activity.info().heartbeat_details`, if present. This handles the
    retry case — the session resumes mid-conversation instead of restarting
    from the first turn.

    On exit, the session's final state is available via the yielded
    :class:`AgenticSession` object.

    Usage::

        async with agentic_session() as session:
            tools = ToolRegistry()
            # ... register handlers ...
            await session.run_tool_loop(
                registry=tools,
                provider="anthropic",
                system=SYSTEM,
                prompt=prompt,
            )
        # session.issues contains all results accumulated during the run

    Retry behavior:
        A 10-turn LLM analysis that crashes on turn 9 resumes from turn 9.
        Token cost on retry is proportional to remaining turns, not total turns.
        This matters for long analyses with many tool calls.

    Note:
        Logs a warning if heartbeat details are present but cannot be decoded;
        the session then starts fresh rather than raising.
    """
    details = activity.info().heartbeat_details
    saved: dict[str, Any] = {}
    if details:
        try:
            saved = json.loads(details[0])
            v = saved.get("version")
            if v is None:
                _logger.warning(
                    "AgenticSession: checkpoint has no version field"
                    " — may be from an older release"
                )
            elif v != 1:
                _logger.warning(
                    "AgenticSession: checkpoint version %s, expected 1 — starting fresh", v
                )
                saved = {}
        except (json.JSONDecodeError, TypeError, IndexError) as e:
            _logger.warning(
                "AgenticSession: failed to decode checkpoint, starting fresh: %s", e
            )

    session = AgenticSession(
        messages=saved.get("messages", []),
        issues=saved.get("issues", []),
    )
    yield session
