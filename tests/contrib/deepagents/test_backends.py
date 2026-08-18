"""``TemporalBackend`` routes real-I/O backend ops through activities.

A backend that touches disk or a shell must not run its operations from workflow
code. ``TemporalBackend`` wraps such a backend so each op becomes a
``deepagents.backend_op`` activity. The wrapped backend here is a plain object
(no LangChain / deepagents needed), so this boots a real server and proves the
op crosses the activity boundary.

A state-only backend needs no wrapping — that path is covered against the real
``deepagents.StateBackend`` when it is importable.
"""

from __future__ import annotations

import gc
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
pytest.importorskip("deepagents")
pytest.importorskip("langchain_core")

from temporalio import workflow
from temporalio.contrib.deepagents import DeepAgentsPlugin, TemporalBackend
from temporalio.contrib.deepagents._tools import (
    register_backend,
    registered_backends,
)
from temporalio.worker import Worker
from tests.contrib.deepagents.helpers import count_scheduled_activities

BACKEND_OP = "deepagents.backend_op"


class RecordingBackend:
    """A minimal backend doing 'real' work off-workflow, exposing both halves
    of the deepagents backend protocol: a sync op (``read``) and its async
    twin (``aread``). The async twin is the regression-critical case —
    deepagents' filesystem middleware calls ``aread``/``awrite``/…, and an
    earlier op list intercepted only sync names, so agent-driven file tools
    ran their I/O in-workflow."""

    def read(self, file_path: str) -> str:
        return f"contents of {file_path}"

    async def aread(self, file_path: str) -> str:
        return f"acontents of {file_path}"


@workflow.defn
class BackendWorkflow:
    @workflow.run
    async def run(self, path: str) -> str:
        backend = TemporalBackend(
            RecordingBackend(),
            activity_options={"start_to_close_timeout": timedelta(seconds=10)},
        )
        sync_out = await backend.read(path)
        async_out = await backend.aread(path)
        return f"{sync_out}|{async_out}"


# Bind deepagents symbols off the module importorskip returns: a static
# `from deepagents import ...` cannot resolve on Python 3.10 (deepagents
# needs >= 3.11), and with the package absent the type checkers mis-resolve
# the name against this same-named test directory.
_deepagents_mod = pytest.importorskip("deepagents")
_backends_mod = pytest.importorskip("deepagents.backends")
create_deep_agent = _deepagents_mod.create_deep_agent
FilesystemBackend = _backends_mod.FilesystemBackend
StateBackend = _backends_mod.StateBackend


# A state-only backend is pure workflow state and must NOT schedule an activity.
@workflow.defn
class StateBackendWorkflow:
    @workflow.run
    async def run(self) -> str:
        backend = StateBackend()
        # Merely holding a StateBackend schedules no activity; it is not
        # wrapped. Return the class provenance so the assertion is on a real
        # runtime property rather than a statically-decidable comparison.
        return type(backend).__module__


# The full agent-level seam: a REAL Deep Agent whose BUILT-IN file tools drive a
# REAL FilesystemBackend through TemporalBackend. This is the path a fake-backend
# test cannot cover: deepagents' filesystem middleware calls the ASYNC protocol
# (`awrite` / `aread`), and the ops return protocol dataclasses (WriteResult /
# ReadResult) that must survive the activity boundary as real objects — the
# middleware reads their attributes in-workflow.
@workflow.defn
class FilesystemAgentWorkflow:
    @workflow.run
    async def run(self, root_dir: str) -> str:
        backend = TemporalBackend(
            FilesystemBackend(root_dir=root_dir, virtual_mode=True),
            activity_options={"start_to_close_timeout": timedelta(seconds=10)},
        )
        agent = create_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            backend=backend,
            system_prompt="Write the note, read it back, then report it.",
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Note 'hello' down."}]}
        )
        return str(result["messages"][-1].content)


@pytest.mark.asyncio
async def test_temporal_backend_op_activity(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-backend",
        workflows=[BackendWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            BackendWorkflow.run,
            "notes.txt",
            id=f"da-backend-{uuid.uuid4()}",
            task_queue="da-backend",
        )
        out = await handle.result()

    assert out == "contents of notes.txt|acontents of notes.txt"
    counts = await count_scheduled_activities(handle)
    # One activity per op — the sync read AND the async aread both cross.
    assert counts[BACKEND_OP] == 2, counts


def test_temporal_backend_unregisters_on_gc() -> None:
    # A wrapper is typically constructed per workflow run; its registry entry
    # must not outlive it, or a long-lived worker leaks one entry per run.
    inner = RecordingBackend()
    before = set(registered_backends())
    wrapper = TemporalBackend(inner)
    (ref,) = set(registered_backends()) - before
    assert registered_backends()[ref] is inner
    del wrapper
    gc.collect()
    assert ref not in registered_backends()


def test_temporal_backend_gc_keeps_reregistered_ref() -> None:
    # Refs are deterministic per run: after a cache eviction, a replay
    # re-registers the SAME ref with a fresh inner backend. The evicted
    # wrapper's GC cleanup must leave that live registration alone.
    before = set(registered_backends())
    wrapper = TemporalBackend(RecordingBackend())
    (ref,) = set(registered_backends()) - before
    replacement = RecordingBackend()
    register_backend(ref, replacement)
    del wrapper
    gc.collect()
    assert registered_backends().get(ref) is replacement
    registered_backends().pop(ref, None)


@pytest.mark.asyncio
async def test_state_backend_in_workflow(env: WorkflowEnvironment) -> None:
    # A state-only backend is pure workflow state and must NOT schedule an
    # activity. Exercised against the real StateBackend when deepagents is present.
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-state-backend",
        workflows=[StateBackendWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            StateBackendWorkflow.run,
            id=f"da-state-backend-{uuid.uuid4()}",
            task_queue="da-state-backend",
        )
        assert (await handle.result()).startswith("deepagents")
    counts = await count_scheduled_activities(handle)
    assert counts[BACKEND_OP] == 0, counts


@pytest.mark.asyncio
async def test_agent_builtin_file_tools_route_backend_ops(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """An unmodified agent's built-in write_file/read_file tools cross the
    activity boundary when the backend is TemporalBackend-wrapped — under
    ``max_cached_workflows=0``, so every workflow task replays from history.

    Regression: an earlier op list intercepted only sync method names, so the
    middleware's async calls (`awrite`/`aread`) forwarded to the inner backend
    and ran real disk I/O in-workflow. This test fails if that recurs, if the
    protocol result dataclasses stop surviving the activity boundary, or if
    replay diverges.
    """
    from langchain_core.messages import AIMessage  # real lib; guarded above

    write_turn = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "write_file",
                "args": {"file_path": "/notes.txt", "content": "hello"},
                "id": "call-write",
            }
        ],
    )
    read_turn = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "read_file",
                "args": {"file_path": "/notes.txt"},
                "id": "call-read",
            }
        ],
    )
    final = AIMessage(content="The note says: hello")
    from temporalio.contrib.deepagents.testing import mock_model_provider

    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider([write_turn, read_turn, final]),
    )
    async with Worker(
        env.client,
        task_queue="da-fs-agent",
        workflows=[FilesystemAgentWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await env.client.start_workflow(
            FilesystemAgentWorkflow.run,
            str(tmp_path),
            id=f"da-fs-agent-{uuid.uuid4()}",
            task_queue="da-fs-agent",
        )
        out = await handle.result()

    assert "hello" in out
    # The write really happened on disk — in the activity, not the workflow.
    assert (tmp_path / "notes.txt").read_text() == "hello"
    counts = await count_scheduled_activities(handle)
    # Exactly one backend_op per file tool call (awrite + aread), three model turns.
    assert counts[BACKEND_OP] == 2, counts
    assert counts["deepagents.invoke_model"] == 3, counts


@workflow.defn
class DefaultBackendGrepWorkflow:
    @workflow.run
    async def run(self, prompt: str) -> str:
        # No backend argument: deepagents uses its default state-only backend.
        agent = create_deep_agent(model="anthropic:claude-sonnet-4-5")
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]}
        )
        return str(result["messages"][-1].content)


@pytest.mark.asyncio
async def test_builtin_tool_on_default_backend_runs_in_workflow(
    env: WorkflowEnvironment,
) -> None:
    """A built-in tool call (grep) on the DEFAULT state backend runs inline
    in the workflow — no activity, no thread hop — under
    ``max_cached_workflows=0`` so every task replays from history.

    Regression: ``BackendProtocol``'s async defaults wrap their sync twins in
    ``asyncio.to_thread``, which the deterministic workflow event loop
    rejects with ``NotImplementedError``. A real model's first spontaneous
    ``grep``/``read_file`` call crashed the workflow task; scripted tests
    that never invoked built-ins on the default backend sailed past it. The
    plugin now runs the sync twin inline when ``workflow.in_workflow()``.
    """
    from langchain_core.messages import AIMessage

    from temporalio.contrib.deepagents.testing import mock_model_provider

    grep_turn = AIMessage(
        content="",
        tool_calls=[{"name": "grep", "args": {"pattern": "hello"}, "id": "call-grep"}],
    )
    final = AIMessage(content="No matches found; done.")
    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider([grep_turn, final]),
    )
    async with Worker(
        env.client,
        task_queue="da-default-backend",
        workflows=[DefaultBackendGrepWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await env.client.start_workflow(
            DefaultBackendGrepWorkflow.run,
            "Grep the workspace for 'hello'.",
            id=f"da-default-backend-{uuid.uuid4()}",
            task_queue="da-default-backend",
        )
        out = await handle.result()

    assert "done" in out
    counts = await count_scheduled_activities(handle)
    # The state-backend op stays in-workflow: model turns are the ONLY activities.
    assert counts[BACKEND_OP] == 0, counts
    assert counts["deepagents.invoke_model"] == 2, counts
