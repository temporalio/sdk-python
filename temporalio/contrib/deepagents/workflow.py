"""Workflow-side surface: the failure type, the dispatch helpers, and the runner.

Everything here runs *inside* the workflow. The dispatch helpers
(:func:`call_model` / :func:`call_tool` / :func:`call_backend_op`) are the single
choke point through which the in-workflow model / tool / backend stubs reach
their activities; they also consult the continue-as-new result cache so work
done before a ``continue_as_new`` is reused rather than repeated after it.

:func:`run_deep_agent` is the optional driver that adds continue-as-new
state-carry around a native ``agent.ainvoke(...)`` — plain ``agent.ainvoke(...)``
still works without it.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from typing import Any

from temporalio import workflow
from temporalio.contrib.deepagents import _activity, _serde
from temporalio.exceptions import ApplicationError

# Reserved key under which the CAN result cache rides inside a state snapshot.
_CACHE_KEY = "__temporal_cache__"

# Checkpointer classes that keep their state in the workflow's own memory and are
# therefore rehydrated for free by deterministic replay. Anything else does its
# own I/O and is not replay-safe from inside the workflow.
_IN_WORKFLOW_SAVERS = frozenset({"InMemorySaver", "MemorySaver"})


def warn_durable_checkpointer(checkpointer: Any) -> None:
    """Warn when a user hands ``create_deep_agent`` a durable checkpointer.

    The Deep Agents loop runs inside the workflow, so a checkpointer that does
    its own database / disk I/O would run that I/O from workflow code — not
    replay-safe. We respect the user's choice (a warning, not a hard failure),
    and point them at the durability path that *is* safe: the default in-workflow
    ``InMemorySaver`` rehydrated by replay, plus
    :func:`run_deep_agent` with ``continue_as_new_after`` for long conversations.
    """
    if checkpointer is None:
        return
    if type(checkpointer).__name__ in _IN_WORKFLOW_SAVERS:
        return
    warnings.warn(
        f"create_deep_agent received a durable checkpointer "
        f"{type(checkpointer).__name__!r}. The agent loop runs inside the "
        f"workflow, so this checkpointer's I/O would run from workflow code, "
        f"which is not replay-safe. Prefer the default in-workflow InMemorySaver "
        f"(rehydrated by replay) plus run_deep_agent(continue_as_new_after=...) "
        f"for long-conversation durability.",
        stacklevel=3,
    )


class DeepAgentsWorkflowError(ApplicationError):
    """Raised for non-retryable Deep Agents failures surfaced in the workflow.

    This is the type registered in the plugin's
    ``workflow_failure_exception_types``, so a model / tool failure that Temporal
    has exhausted (or an invalid agent configuration) fails the workflow with a
    stable ``ApplicationError.type`` — never a stringified peer exception.
    """

    TYPE = "deepagents.DeepAgentsWorkflowError"

    def __init__(self, message: str, *, non_retryable: bool = True) -> None:
        """Construct the error with the plugin's stable failure ``type``."""
        super().__init__(message, type=self.TYPE, non_retryable=non_retryable)


# ---------------------------------------------------------------------------
# Dispatch helpers (the model / tool / backend choke point)
# ---------------------------------------------------------------------------


async def call_model(
    activity_name: str,
    activity_input: _activity.ModelActivityInput,
    *,
    summary: str,
    **opts: Any,
) -> _activity.ModelActivityOutput:
    """Dispatch one model call, reusing a cached result across continue-as-new."""
    key = _serde.cache_key(
        "model",
        activity_input.model_name,
        [activity_input.messages, activity_input.tool_schemas],
    )
    hit, cached = _serde.cache_lookup(key)
    if hit:
        return _activity.ModelActivityOutput(message=cached)
    output = await workflow.execute_activity(
        activity_name,
        activity_input,
        result_type=_activity.ModelActivityOutput,
        summary=summary,
        **opts,
    )
    _serde.cache_put(key, output.message)
    return output


async def call_tool(
    activity_input: _activity.ToolActivityInput,
    *,
    summary: str,
    **opts: Any,
) -> _activity.ToolActivityOutput:
    """Dispatch one tool call, reusing a cached result across continue-as-new."""
    key = _serde.cache_key("tool", activity_input.tool_name, activity_input.args)
    hit, cached = _serde.cache_lookup(key)
    if hit:
        return _activity.ToolActivityOutput(message=cached)
    output = await workflow.execute_activity(
        _activity.INVOKE_TOOL,
        activity_input,
        result_type=_activity.ToolActivityOutput,
        summary=summary,
        **opts,
    )
    _serde.cache_put(key, output.message)
    return output


async def call_backend_op(
    activity_input: _activity.BackendOpInput,
    *,
    summary: str,
    **opts: Any,
) -> _activity.BackendOpOutput:
    """Dispatch one backend op, reusing a cached result across continue-as-new."""
    key = _serde.cache_key(
        f"backend:{activity_input.backend_ref}",
        activity_input.op,
        [activity_input.args, activity_input.kwargs],
    )
    hit, cached = _serde.cache_lookup(key)
    if hit:
        return _activity.BackendOpOutput(result=cached)
    output = await workflow.execute_activity(
        _activity.BACKEND_OP,
        activity_input,
        result_type=_activity.BackendOpOutput,
        summary=summary,
        **opts,
    )
    _serde.cache_put(key, output.result)
    return output


# ---------------------------------------------------------------------------
# run_deep_agent (continue-as-new state carry)
# ---------------------------------------------------------------------------


def _merge_snapshot(input: Any, snapshot: Mapping[str, Any]) -> Any:
    """Prepend a snapshot's carried messages onto the next turn's input."""
    raw_prior: Any = snapshot.get("messages") or []
    prior = list(raw_prior)
    if not prior:
        return input
    if isinstance(input, Mapping):
        merged = dict(input)
        raw_next: Any = input.get("messages") or []
        merged["messages"] = [*prior, *list(raw_next)]
        return merged
    return {"messages": [*prior, *_as_message_list(input)]}


def _as_message_list(input: Any) -> list[Any]:
    if isinstance(input, (list, tuple)):
        return list(input)
    return [input]


async def run_deep_agent(
    agent: Any,
    input: Any,
    *,
    continue_as_new_after: int | None = None,
    state_snapshot: Mapping[str, Any] | None = None,
) -> Any:
    """Drive ``agent.ainvoke(input)`` with optional continue-as-new state carry.

    Without ``continue_as_new_after`` this is a thin wrapper over
    ``agent.ainvoke``. With it, once the workflow history passes the threshold the
    completed turn's state (messages + the model/tool result cache) is snapshotted
    and carried into a fresh run via ``workflow.continue_as_new``, so long
    conversations do not accumulate unbounded history.

    The enclosing ``@workflow.run`` method must accept the continued call — i.e.
    its signature is ``(input, state_snapshot=None)`` — because that is how the
    carried state is threaded into the next run.
    """
    # Resume path: rehydrate the result cache and fold carried messages in.
    if state_snapshot is not None:
        _serde.set_result_cache(dict(state_snapshot.get(_CACHE_KEY) or {}))
        input = _merge_snapshot(input, state_snapshot)
    else:
        _serde.set_result_cache({})

    try:
        result = await agent.ainvoke(input)
    except ApplicationError:
        raise
    except Exception as exc:
        # If the framework wrapped a Temporal failure, surface the registered
        # workflow-failure type rather than the framework's generic exception.
        cause = exc.__cause__
        if cause is not None and workflow.is_failure_exception(cause):
            raise DeepAgentsWorkflowError(f"Deep Agents run failed: {exc}") from cause
        raise

    if (
        continue_as_new_after is not None
        and workflow.info().get_current_history_length() >= continue_as_new_after
        and _has_pending_work(result)
    ):
        snapshot = {
            "messages": _extract_messages(result),
            _CACHE_KEY: _serde.result_cache_snapshot() or {},
        }
        # ``continue_as_new`` threads positional args into the next run via
        # ``args=``; the enclosing ``@workflow.run`` receives them as
        # ``(input, state_snapshot)``.
        workflow.continue_as_new(args=[input, snapshot])

    return result


def _extract_messages(result: Any) -> list[Any]:
    if isinstance(result, Mapping):
        raw: Any = result.get("messages") or []
        return list(raw)
    return []


def _has_pending_work(result: Any) -> bool:
    """True when the agent left unfinished todos worth carrying past a CAN.

    A finished single-shot run has no pending todos, so this returns False and the
    driver returns the result instead of looping on continue-as-new forever.
    """
    if isinstance(result, Mapping):
        todos: Any = result.get("todos") or []
        return any(
            isinstance(t, Mapping) and t.get("status") not in ("completed", "done")
            for t in todos
        )
    return False
