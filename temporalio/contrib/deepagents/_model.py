"""The model seam: a chat model whose every call becomes a Temporal activity.

:class:`TemporalModel` is a real ``BaseChatModel``. Placed anywhere Deep Agents
expects a model, it routes each generation through the ``deepagents.invoke_model``
activity instead of calling the provider inline. Because Deep Agents' sub-agents
inherit the parent's ``model`` instance by default, substituting this one object
makes every model call in the whole agent tree durable — no middleware injection
that a sub-agent could silently miss.

Two ways to get one:

* explicit — the user writes ``TemporalModel("anthropic:claude-...")`` and hands
  it to ``create_deep_agent(model=...)``; this is the public type users assert
  against;
* implicit — while running inside a workflow, the plugin patches
  ``create_deep_agent`` so a bare ``model="anthropic:..."`` string is wrapped
  automatically (see :func:`install_model_patch`).

Worker-wide dispatch defaults (activity options, streaming topic) live in
:class:`temporalio.contrib.deepagents._serde.Settings`, a module global set by
the plugin. They live in ``_serde`` (not here) so the plugin can configure them
without importing this module — which would drag LangChain into plugin
construction. This module is under ``temporalio``, which the workflow sandbox
passes through, so the object the workflow reads is the same one the plugin set.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.contrib.deepagents import _activity, _serde

with workflow.unsafe.imports_passed_through():
    from langchain_core.callbacks import (
        AsyncCallbackManagerForLLMRun,
        CallbackManagerForLLMRun,
    )
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessageChunk, BaseMessage
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult


def _as_message_chunk(message: Any) -> Any:
    """Re-shape a finished ``AIMessage`` as the ``AIMessageChunk`` a stream yields."""
    return AIMessageChunk(
        content=getattr(message, "content", ""),
        additional_kwargs=getattr(message, "additional_kwargs", {}) or {},
        response_metadata=getattr(message, "response_metadata", {}) or {},
        id=getattr(message, "id", None),
        tool_calls=getattr(message, "tool_calls", []) or [],
        usage_metadata=getattr(message, "usage_metadata", None),
    )


# ---------------------------------------------------------------------------
# Activity-option resolution (worker-wide defaults live in _serde.Settings)
# ---------------------------------------------------------------------------


_DEFAULT_MODEL_TIMEOUT = timedelta(minutes=5)


def _resolve_activity_options(
    model_name: str, instance_options: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Merge worker defaults with a per-instance override into execute_activity kwargs."""
    opts: dict[str, Any] = {}
    default = _serde.get_settings().model_activity_options
    if isinstance(default, Mapping) and not _looks_like_activity_config(default):
        # Mapping[model_name, ActivityConfig]: pick this model's entry.
        chosen = default.get(model_name)
        if isinstance(chosen, Mapping):
            opts.update(chosen)
    elif isinstance(default, Mapping):
        opts.update(default)
    if instance_options:
        opts.update(instance_options)
    opts.setdefault("start_to_close_timeout", _DEFAULT_MODEL_TIMEOUT)
    return opts


def _looks_like_activity_config(m: Mapping[str, Any]) -> bool:
    """A single ``ActivityConfig`` has known option keys; a per-model map does not."""
    known = {
        "task_queue",
        "schedule_to_close_timeout",
        "schedule_to_start_timeout",
        "start_to_close_timeout",
        "heartbeat_timeout",
        "retry_policy",
        "cancellation_type",
        "activity_id",
        "versioning_intent",
        "summary",
        "priority",
    }
    return bool(m) and all(k in known for k in m)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


class TemporalModel(BaseChatModel):
    """A ``BaseChatModel`` that runs each generation as a Temporal activity.

    Args:
        model: The provider model name resolved worker-side by the plugin's
            ``model_provider`` (e.g. ``"anthropic:claude-sonnet-4-5"``). Only the
            name crosses the workflow boundary; credentials stay on the worker.
        activity_options: Optional per-model ``execute_activity`` overrides
            (timeouts, retry policy). Falls back to the plugin's
            ``model_activity_options``.
    """

    model: str
    activity_options: dict[str, Any] | None = None

    # ``protected_namespaces=()`` silences pydantic's warning about the ``model``
    # field colliding with its ``model_`` namespace.
    model_config = {"arbitrary_types_allowed": True, "protected_namespaces": ()}

    @property
    def _llm_type(self) -> str:
        return "temporal-deepagents"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        """Bind tools to the model the way LangChain's ``create_agent`` expects.

        ``BaseChatModel.bind_tools`` is abstract (raises ``NotImplementedError``),
        but the agent factory calls ``model.bind_tools(tools, ...)`` on every model
        node — so a durable model must implement it or the whole loop dies. The
        tools are converted to their JSON schema *now* (at bind time) so the bound
        object is serialization-safe, and carried as the ``tools`` kwarg that
        :meth:`_build_input` already reads and forwards to the activity, where the
        real provider model is what actually binds them.
        """
        schemas = [
            t if isinstance(t, dict) else _serde.tool_to_schema(t) for t in tools
        ]
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return self.bind(tools=schemas, **kwargs)

    def _summary(self) -> str:
        return f"invoke_model[{self.model}]"

    def _build_input(
        self,
        messages: Sequence[BaseMessage],
        streaming_topic: str | None,
        **kwargs: Any,
    ) -> _activity.ModelActivityInput:
        tools = kwargs.get("tools") or []
        tool_schemas = [
            t if isinstance(t, dict) else _serde.tool_to_schema(t) for t in tools
        ]
        bind_kwargs = {
            k: v
            for k, v in kwargs.items()
            if k not in ("tools", "config", "run_manager", "stop", "callbacks")
            and _serde._is_jsonish(v)
        }
        if kwargs.get("stop"):
            bind_kwargs["stop"] = kwargs["stop"]
        return _activity.ModelActivityInput(
            model_name=self.model,
            messages=_serde.dump_messages(messages),
            tool_schemas=tool_schemas,
            bind_kwargs=bind_kwargs,
            config=_serde.strip_runnable_config(kwargs.get("config") or {}),
            streaming_topic=streaming_topic,
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError(
            "TemporalModel is async-only inside a Temporal workflow. Drive the "
            "agent with `await agent.ainvoke(...)` / `.astream(...)`, which uses "
            "the async model path."
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        from temporalio.contrib.deepagents.workflow import call_model

        activity_input = self._build_input(messages, None, stop=stop, **kwargs)
        opts = _resolve_activity_options(self.model, self.activity_options)
        output = await call_model(
            _activity.INVOKE_MODEL,
            activity_input,
            summary=self._summary(),
            **opts,
        )
        message = _serde.load_object(output.message)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        from temporalio.contrib.deepagents.workflow import call_model

        topic = _serde.get_settings().streaming_topic
        opts = _resolve_activity_options(self.model, self.activity_options)
        if topic:
            activity_input = self._build_input(messages, topic, stop=stop, **kwargs)
            output = await call_model(
                _activity.INVOKE_MODEL_STREAMING,
                activity_input,
                summary=f"invoke_model_streaming[{self.model}]",
                **opts,
            )
        else:
            # No topic configured: fall back to a single response-level chunk.
            activity_input = self._build_input(messages, None, stop=stop, **kwargs)
            output = await call_model(
                _activity.INVOKE_MODEL,
                activity_input,
                summary=self._summary(),
                **opts,
            )
        message = _serde.load_object(output.message)
        yield ChatGenerationChunk(message=_as_message_chunk(message))

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        raise NotImplementedError(
            "TemporalModel streaming is async-only; use `agent.astream(...)`."
        )


# ---------------------------------------------------------------------------
# create_deep_agent model patch (implicit wrapping)
# ---------------------------------------------------------------------------
#
# The durability seam is ``deepagents._models.resolve_model``, which
# ``create_deep_agent`` calls to turn a ``model=`` string (or instance) into a
# ``BaseChatModel`` — for both the top-level agent and every ``SubAgent``
# (``graph.py`` lines 592 and 634). We patch it on the ``deepagents.graph``
# module, where ``create_deep_agent``'s body resolves the ``resolve_model`` name
# at call time.
#
# Patching *this* seam (not ``deepagents.create_deep_agent``) is what makes the
# rewrite survive the user's import style. A user who writes the idiomatic
# ``from deepagents import create_deep_agent`` binds the *original* function
# object into their module; rebinding the ``deepagents.create_deep_agent``
# attribute would never be seen by that already-bound reference, so string
# models would reach the real provider inside the workflow (a hang / non-
# determinism). ``create_deep_agent``'s body, by contrast, always looks up
# ``resolve_model`` in the ``deepagents.graph`` globals afresh on each call, so
# rebinding it there is observed no matter how the caller imported the factory.
# It also preserves ``_model_spec`` (the original string), which the factory
# reads *before* calling ``resolve_model`` for harness-profile lookup.
#
# ``create_deep_agent`` is still wrapped separately, best-effort, purely to fire
# the construction-time warnings that need the ``tools`` / ``checkpointer``
# kwargs (those warnings are advisory and carry no durability weight).

_original_create_deep_agent: Any = None
_original_resolve_model: Any = None


def _wrap_model_arg(model: Any) -> Any:
    """Resolve a ``create_deep_agent`` model argument to a durable model.

    A name string becomes a :class:`TemporalModel`. A :class:`TemporalModel`
    passes through. Any other live ``BaseChatModel`` instance is rejected at the
    workflow boundary: it has no name the activity could rebuild from, so it would
    run its provider call *inside the workflow* — nondeterministic and unsafe.
    """
    from temporalio.contrib.deepagents.workflow import DeepAgentsWorkflowError

    if isinstance(model, str):
        return TemporalModel(model=model)
    if isinstance(model, TemporalModel):
        return model
    if isinstance(model, BaseChatModel):
        raise DeepAgentsWorkflowError(
            f"create_deep_agent received a live {type(model).__name__} model "
            f"instance, which would run inside the workflow (nondeterministic). "
            f'Pass model="provider:name" (auto-routed through an activity) or '
            f"wrap it as TemporalModel(...) instead."
        )
    return model


def install_model_patch() -> None:
    """Route Deep Agents' model resolution through :class:`TemporalModel`.

    Patches ``deepagents.graph.resolve_model`` (the seam ``create_deep_agent``
    uses for the main agent *and* every sub-agent) so a bare ``model="..."``
    string becomes a durable :class:`TemporalModel`, and additionally wraps
    ``deepagents.create_deep_agent`` to fire the advisory tool / checkpointer
    warnings. Both only act when called inside a workflow, so importing
    deepagents on a plain client / activity worker is unaffected. Idempotent.
    """
    global _original_create_deep_agent, _original_resolve_model
    # importlib: `deepagents` is absent on Python 3.10 environments (its floor
    # is 3.11), so static imports here fail type-checking there.
    deepagents = importlib.import_module("deepagents")
    _graph = importlib.import_module("deepagents.graph")

    if _original_resolve_model is None:
        _original_resolve_model = _graph.resolve_model

        def patched_resolve_model(model: Any) -> Any:
            if workflow.in_workflow():
                return _wrap_model_arg(model)
            return _original_resolve_model(model)

        setattr(_graph, "resolve_model", patched_resolve_model)

    if _original_create_deep_agent is None:
        _original_create_deep_agent = deepagents.create_deep_agent

        def patched(*args: Any, **kwargs: Any) -> Any:
            if workflow.in_workflow():
                from temporalio.contrib.deepagents._tools import warn_unwrapped_tools
                from temporalio.contrib.deepagents.workflow import (
                    warn_durable_checkpointer,
                )

                warn_unwrapped_tools(kwargs.get("tools"))
                warn_durable_checkpointer(kwargs.get("checkpointer"))
            return _original_create_deep_agent(*args, **kwargs)

        setattr(deepagents, "create_deep_agent", patched)


def uninstall_model_patch() -> None:
    """Restore the original ``resolve_model`` / ``create_deep_agent``."""
    global _original_create_deep_agent, _original_resolve_model
    if _original_resolve_model is not None:
        _graph = importlib.import_module("deepagents.graph")

        setattr(_graph, "resolve_model", _original_resolve_model)
        _original_resolve_model = None
    if _original_create_deep_agent is not None:
        deepagents = importlib.import_module("deepagents")

        setattr(deepagents, "create_deep_agent", _original_create_deep_agent)
        _original_create_deep_agent = None
