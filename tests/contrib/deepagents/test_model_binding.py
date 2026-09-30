"""Worker-side model binding: bind kwargs and tool schemas must both reach the provider.

These exercise ``DeepAgentActivities._build_bound_model`` directly (no workflow and no
Temporal server needed), so they depend only on LangChain.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from typing_extensions import override

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)

pytest.importorskip("langchain_core")

from langchain_core.language_models.chat_models import (  # noqa: E402
    BaseChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.runnables import Runnable  # noqa: E402
from langchain_core.tools import BaseTool  # noqa: E402

from temporalio.contrib.deepagents._activity import (  # noqa: E402
    DeepAgentActivities,
    ModelActivityInput,
)

_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "memory_recall",
        "description": "search memory",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "EmailNeed",
        "schema": {
            "type": "object",
            "properties": {"request": {"type": "string"}},
            "required": ["request"],
        },
    },
}


class _RecordingChatModel(BaseChatModel):
    """Chat model that records the kwargs a bound runnable passes down to it."""

    received: list[dict[str, Any]]
    """Per-instance record of the kwargs seen by ``_generate``."""

    @override
    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(kwargs)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    @override
    def bind_tools(  # type: ignore[override]
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        # Same shape as provider implementations: bind_tools is a bind() specialization.
        return self.bind(tools=list(tools), tool_choice=tool_choice, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "recording-chat-model"


def test_build_bound_model_keeps_bind_kwargs_when_binding_tools() -> None:
    """Binding tools must not drop earlier bind kwargs (issue #1896).

    ``bind()`` returns a ``RunnableBinding``; calling ``bind_tools()`` on that delegates
    to the unbound model through ``RunnableBinding.__getattr__``, so a tools-only binding
    used to silently drop every bind kwarg (notably ``response_format``, which
    ``ProviderStrategy`` structured output relies on).
    """
    model = _RecordingChatModel(received=[])
    activities = DeepAgentActivities(model_provider=lambda _name: model)

    bound = activities._build_bound_model(
        ModelActivityInput(
            model_name="fake-model",
            messages=[],
            tool_schemas=[_TOOL],
            bind_kwargs={"response_format": _RESPONSE_FORMAT},
        )
    )

    bound.invoke([HumanMessage(content="hi")])

    assert model.received, "the bound model was never invoked"
    received = model.received[0]
    assert received["tools"] == [_TOOL]
    assert received["response_format"] == _RESPONSE_FORMAT


def test_build_bound_model_without_bind_kwargs() -> None:
    """Tools alone still reach the provider when no bind kwargs are supplied."""
    model = _RecordingChatModel(received=[])
    activities = DeepAgentActivities(model_provider=lambda _name: model)

    bound = activities._build_bound_model(
        ModelActivityInput(
            model_name="fake-model",
            messages=[],
            tool_schemas=[_TOOL],
        )
    )

    bound.invoke([HumanMessage(content="hi")])

    assert model.received[0]["tools"] == [_TOOL]
    assert "response_format" not in model.received[0]
