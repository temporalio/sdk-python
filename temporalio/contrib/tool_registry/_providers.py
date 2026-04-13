"""LLM provider implementations for ToolRegistry.

Provides :class:`AnthropicProvider` and :class:`OpenAIProvider`, each
implementing a complete multi-turn tool-calling loop.  The top-level
:func:`run_tool_loop` function constructs the appropriate provider and runs
the loop.

Both providers follow the same protocol:

1. Send messages + tool definitions to the model.
2. If the model returns ``tool_use`` / ``function`` blocks, dispatch each
   tool call via :meth:`ToolRegistry.dispatch` and append the result.
3. Repeat until the model returns a ``stop_reason`` of ``"end_turn"`` /
   ``"stop"`` with no tool calls, or raises :exc:`StopIteration`.

The providers are *not* intended to be used directly; prefer :func:`run_tool_loop`
or :class:`_session.AgenticSession`.
"""

from __future__ import annotations

import json
import os
from typing import Any

from temporalio.contrib.tool_registry._registry import ToolRegistry


def _blocks_to_dicts(content: Any) -> list[dict[str, Any]]:
    """Convert an Anthropic response content list to plain JSON-serialisable dicts.

    Anthropic returns Pydantic ``ContentBlock`` objects.  Before storing them
    in heartbeat state they must be converted to plain ``dict`` instances.
    """
    if isinstance(content, list):
        result = []
        for item in content:
            if isinstance(item, dict):
                result.append(item)
            elif hasattr(item, "model_dump"):
                result.append(item.model_dump())
            elif hasattr(item, "__dict__"):
                result.append(dict(vars(item)))
            else:
                result.append({"type": "text", "text": str(item)})
        return result
    return [{"type": "text", "text": str(content)}]


class AnthropicProvider:
    """Multi-turn Anthropic tool-calling loop.

    Args:
        registry: Tool registry whose handlers are called on each tool-use block.
        system: System prompt.
        client: An ``anthropic.Anthropic`` client instance.  If ``None``, one
            is constructed from the ``ANTHROPIC_API_KEY`` environment variable.
        model: Model name (default: ``claude-sonnet-4-6``).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        system: str,
        client: Any = None,
        model: str = "claude-sonnet-4-6",
    ) -> None:
        """Initialize AnthropicProvider."""
        self._registry = registry
        self._system = system
        self._model = model
        if client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        else:
            self._client = client

    def run_turn(self, messages: list[dict[str, Any]]) -> bool:
        """Execute one turn of the conversation.

        Appends the assistant response (and any tool results) to *messages*
        in-place.

        Returns:
            ``True`` when the loop should stop (no more tool calls), ``False``
            to continue.
        """
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=self._system,
            tools=self._registry.to_anthropic(),  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
        )

        assistant_content = _blocks_to_dicts(response.content)
        messages.append({"role": "assistant", "content": assistant_content})

        tool_calls = [b for b in assistant_content if b.get("type") == "tool_use"]
        if not tool_calls or response.stop_reason == "end_turn":
            return True  # done

        tool_results = []
        for call in tool_calls:
            is_error = False
            try:
                result = self._registry.dispatch(call["name"], call.get("input", {}))
            except Exception as e:
                result = f"error: {e}"
                is_error = True
            entry: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call["id"],
                "content": result,
            }
            if is_error:
                entry["is_error"] = True
            tool_results.append(entry)
        messages.append({"role": "user", "content": tool_results})
        return False

    def run_loop(self, messages: list[dict[str, Any]]) -> None:
        """Run turns until the model stops using tools."""
        while not self.run_turn(messages):
            pass


class OpenAIProvider:
    """Multi-turn OpenAI function-calling loop.

    Args:
        registry: Tool registry whose handlers are called on each function call.
        system: System prompt.
        client: An ``openai.OpenAI`` client instance.  If ``None``, one is
            constructed from the ``OPENAI_API_KEY`` environment variable.
        model: Model name (default: ``gpt-4o``).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        system: str,
        client: Any = None,
        model: str = "gpt-4o",
    ) -> None:
        """Initialize OpenAIProvider."""
        self._registry = registry
        self._system = system
        self._model = model
        if client is None:
            import openai

            self._client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        else:
            self._client = client

    def run_turn(self, messages: list[dict[str, Any]]) -> bool:
        """Execute one turn of the conversation.

        Appends the assistant response (and any tool results) to *messages*
        in-place.

        Returns:
            ``True`` when the loop should stop, ``False`` to continue.
        """
        # Prepend system message for OpenAI format
        full_messages = [{"role": "system", "content": self._system}] + messages

        response = self._client.chat.completions.create(
            model=self._model,
            tools=self._registry.to_openai(),  # type: ignore[arg-type]
            messages=full_messages,  # type: ignore[arg-type]
        )

        choice = response.choices[0]
        message = choice.message

        # Convert to plain dict for heartbeat-safe storage
        msg_dict: dict[str, Any] = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            msg_dict["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
                if tc.type == "function"
            ]
        messages.append(msg_dict)

        if not message.tool_calls or choice.finish_reason in ("stop", "length"):
            return True

        for tc in message.tool_calls:
            if tc.type != "function":
                continue
            args = json.loads(tc.function.arguments or "{}")
            try:
                result = self._registry.dispatch(tc.function.name, args)
            except Exception as e:
                result = f"error: {e}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )
        return False

    def run_loop(self, messages: list[dict[str, Any]]) -> None:
        """Run turns until the model stops calling functions."""
        while not self.run_turn(messages):
            pass


async def run_tool_loop(
    *,
    provider: str,
    system: str,
    prompt: str,
    tools: ToolRegistry,
    messages: list[dict[str, Any]] | None = None,
    model: str | None = None,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Run a complete multi-turn LLM tool-calling loop.

    This is the primary entry point for simple (non-resumable) tool loops.
    For resumable agentic sessions with crash-safe heartbeating, use
    :class:`_session.AgenticSession` via :func:`_session.agentic_session`.

    Args:
        provider: LLM provider — ``"anthropic"`` or ``"openai"``.
        system: System prompt.
        prompt: Initial user message.
        tools: Registered tool handlers.
        messages: Existing message history to continue from.  If ``None``, a
            new conversation is started with ``prompt`` as the first message.
        model: Model name override.  If ``None``, the provider default is used.
        client: Pre-constructed LLM client.  Useful in tests.

    Returns:
        The final ``messages`` list with the complete conversation history.

    Raises:
        ValueError: If ``provider`` is not ``"anthropic"`` or ``"openai"``.
    """
    if messages is None:
        messages = [{"role": "user", "content": prompt}]
    elif not messages:
        messages = [{"role": "user", "content": prompt}]

    kwargs: dict[str, Any] = {}
    if model is not None:
        kwargs["model"] = model
    if client is not None:
        kwargs["client"] = client

    if provider == "anthropic":
        AnthropicProvider(tools, system, **kwargs).run_loop(messages)
    elif provider == "openai":
        OpenAIProvider(tools, system, **kwargs).run_loop(messages)
    else:
        raise ValueError(f"Unknown provider {provider!r}. Use 'anthropic' or 'openai'.")
    return messages
