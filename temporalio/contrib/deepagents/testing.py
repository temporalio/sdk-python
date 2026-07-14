"""Test helpers for users adopting :class:`DeepAgentsPlugin`.

Unit-testing a Deep Agent under Temporal should not require a live LLM endpoint
or a paid API key. This module ships:

* :class:`FakeModel` — a real ``BaseChatModel`` returning scripted replies (plain
  text or full ``AIMessage``s carrying ``tool_calls``), cycling when exhausted;
* :func:`fake_model_factory` — a one-liner for the common text-only case;
* :func:`mock_model_provider` — a ``model_provider`` (name → model) you pass to
  ``DeepAgentsPlugin(model_provider=...)`` so the model activity runs offline;
* :class:`MockTool` — a scripted ``BaseTool`` for exercising the tool seam.

Importing this module has no process-wide side effects.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from pydantic import PrivateAttr

__all__ = ["FakeModel", "fake_model_factory", "mock_model_provider", "MockTool"]

Response = str | AIMessage


class FakeModel(BaseChatModel):
    """A ``BaseChatModel`` returning scripted responses, for offline tests.

    Args:
        responses: Replies returned one per call, cycling when exhausted. Each is
            either a string (becomes an ``AIMessage``) or an ``AIMessage`` (so you
            can script ``tool_calls`` to drive the agent's tool path).
    """

    responses: list[Any]
    _cursor: int = PrivateAttr(default=0)

    def __init__(self, responses: Sequence[Response], **kwargs: Any) -> None:
        """Validate and store the scripted responses."""
        resp = list(responses)
        if not resp:
            raise ValueError("FakeModel needs at least one scripted response.")
        super().__init__(responses=resp, **kwargs)  # type: ignore[call-arg]

    @property
    def _llm_type(self) -> str:
        return "temporalio-deepagents-fake-model"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "FakeModel":
        """Ignore the tool set; the fake just replays its script.

        Returning ``self`` keeps ``model.bind_tools(...)`` chainable like a
        real model.
        """
        return self

    def _next(self) -> AIMessage:
        item = self.responses[self._cursor % len(self.responses)]
        self._cursor += 1
        return item if isinstance(item, AIMessage) else AIMessage(content=item)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next())])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next())])


def fake_model_factory(responses: Sequence[Response]) -> FakeModel:
    """One-liner scripted fake chat model.

    Example::

        model = fake_model_factory(["The capital of France is Paris."])
    """
    return FakeModel(responses)


def mock_model_provider(
    responses: Sequence[Response],
) -> Callable[[str], FakeModel]:
    """A ``model_provider`` that hands out the scripted responses one call at a time.

    Each model activity invocation (main agent, a sub-agent, a follow-up turn
    after a tool call) advances through ``responses`` and cycles when exhausted,
    so a multi-turn agent can be scripted deterministically. The model activity
    builds a fresh model per call, so the cursor lives on the provider closure
    (shared for the worker's lifetime) rather than on any one model instance.

    Pass to the plugin so the model activity runs offline::

        plugin = DeepAgentsPlugin(model_provider=mock_model_provider(["Paris."]))
    """
    resp = list(responses)
    if not resp:
        raise ValueError("mock_model_provider needs at least one response.")
    cursor = {"i": 0}

    def provider(_model_name: str) -> FakeModel:
        reply = resp[cursor["i"] % len(resp)]
        cursor["i"] += 1
        return FakeModel([reply])

    return provider


class MockTool(BaseTool):
    """A scripted ``BaseTool`` whose call returns a fixed value, for tests."""

    name: str = "mock_tool"
    description: str = "A mock tool that returns a scripted result."
    result: Any = "ok"

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        return self.result

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        return self.result
