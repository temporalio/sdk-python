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
import inspect
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
pytest.importorskip("deepagents")
pytest.importorskip("langchain_core")

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.contrib.deepagents import DeepAgentsPlugin, TemporalBackend
from temporalio.contrib.deepagents._tools import (
    lookup_backend,
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

    # ``delete`` is optional in the protocol (deepagents >= 0.7); a backend that
    # has it must still cross the activity boundary like every other op.
    async def adelete(self, file_path: str) -> str:
        return f"deleted {file_path}"

    def delete(self, file_path: str) -> str:
        return f"deleted {file_path}"


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
        deleted = await backend.adelete(path)
        return f"{sync_out}|{async_out}|{deleted}"


# Bind deepagents symbols off the module importorskip returns: a static
# `from deepagents import ...` cannot resolve on Python 3.10 (deepagents
# needs >= 3.11), and with the package absent the type checkers mis-resolve
# the name against this same-named test directory.
_deepagents_mod = pytest.importorskip("deepagents")
_backends_mod = pytest.importorskip("deepagents.backends")
create_deep_agent = _deepagents_mod.create_deep_agent
FilesystemBackend = _backends_mod.FilesystemBackend
LocalShellBackend = _backends_mod.LocalShellBackend
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

    assert out == "contents of notes.txt|acontents of notes.txt|deleted notes.txt"
    counts = await count_scheduled_activities(handle)
    # One activity per op — the sync read, the async aread, and the optional
    # adelete all cross.
    assert counts[BACKEND_OP] == 3, counts


def test_temporal_backend_mirrors_inner_delete_support() -> None:
    # deepagents decides delete support from the wrapper CLASS, so a wrapper
    # around a delete-capable backend must advertise it and one around a
    # backend without delete must not (or the agent gets a delete tool that
    # can only fail).
    protocol = pytest.importorskip("deepagents.backends.protocol")

    class NoDelete:
        def read(self, file_path: str) -> str:
            return f"contents of {file_path}"

    with_delete = TemporalBackend(RecordingBackend())
    without_delete = TemporalBackend(NoDelete())
    assert protocol._supports_delete(with_delete) is True
    assert protocol._supports_delete(without_delete) is False
    assert isinstance(with_delete, TemporalBackend)
    assert isinstance(without_delete, TemporalBackend)
    # The real deepagents backends resolve the same way wrapped or not.
    state_backend = StateBackend()
    assert protocol._supports_delete(
        TemporalBackend(state_backend)
    ) is protocol._supports_delete(state_backend)


def test_temporal_backend_mirrors_inner_execution_support(tmp_path: Path) -> None:
    # deepagents offers its shell tool when the backend passes an isinstance
    # check against the sandbox protocol, i.e. when execute/aexecute exist on
    # the object. A wrapper must only grow them when the inner backend is
    # execution-capable, or a plain filesystem backend gets a shell tool whose
    # every call fails in the activity.
    supports_execution = pytest.importorskip(
        "deepagents.middleware.filesystem"
    ).supports_execution

    plain = TemporalBackend(
        FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    )
    assert supports_execution(plain) is False
    assert not hasattr(plain, "aexecute")

    shell_inner = LocalShellBackend(root_dir=str(tmp_path))
    shell = TemporalBackend(shell_inner)
    assert supports_execution(shell) is True
    # The op is the activity dispatcher, not the inner backend's own method.
    assert getattr(type(shell), "aexecute") is not type(shell_inner).aexecute
    assert supports_execution(shell_inner) is True


def test_temporal_backend_mirrors_execute_timeout_support(tmp_path: Path) -> None:
    # deepagents gates the execute tool's per-command ``timeout`` on a
    # SIGNATURE probe: ``execute_accepts_timeout(type(backend))`` looks for a
    # ``timeout`` parameter on ``execute`` and, unlike the ``max_count`` probe,
    # does not take ``**kwargs`` as a stand-in. A bare ``(*args, **kwargs)``
    # dispatcher read False, so a wrapped LocalShellBackend refused a timeout
    # its unwrapped self accepts.
    protocol = pytest.importorskip("deepagents.backends.protocol")

    shell_inner = LocalShellBackend(root_dir=str(tmp_path))
    shell = TemporalBackend(shell_inner)
    assert protocol.execute_accepts_timeout(type(shell_inner)) is True
    assert protocol.execute_accepts_timeout(type(shell)) is True
    for op in ("execute", "aexecute"):
        assert inspect.signature(getattr(type(shell), op)) == inspect.signature(
            getattr(type(shell_inner), op)
        )
    # The ``max_count`` probe, which does accept ``**kwargs``, still passes.
    assert protocol._method_accepts_max_count(type(shell), "grep") is True

    # A sandbox whose execute takes no timeout (deepagents' "older backend
    # package" case) must read False wrapped too: deepagents then declines the
    # call up front instead of the forwarded keyword failing inside the
    # activity. Built with type(): a class statement cannot name its base off
    # the importorskip module for the type checkers.
    def execute_without_timeout(_self: Any, command: str) -> str:
        return command

    NoTimeoutSandbox = type(
        "NoTimeoutSandbox",
        (protocol.SandboxBackendProtocol,),
        {"execute": execute_without_timeout},
    )
    no_timeout = TemporalBackend(NoTimeoutSandbox())
    assert protocol.execute_accepts_timeout(NoTimeoutSandbox) is False
    assert protocol.execute_accepts_timeout(type(no_timeout)) is False
    assert type(no_timeout) is not type(shell)


def test_temporal_backend_subclass_keeps_capability_mirroring() -> None:
    protocol = pytest.importorskip("deepagents.backends.protocol")

    class MyBackend(TemporalBackend):
        def extra(self) -> str:
            return "extra"

    class NoDelete:
        def read(self, file_path: str) -> str:
            return f"contents of {file_path}"

    class OwnDelete(TemporalBackend):
        def delete(self, file_path: str) -> str:
            return f"own {file_path}"

    wrapped = MyBackend(RecordingBackend())
    assert isinstance(wrapped, MyBackend)
    assert wrapped.extra() == "extra"
    assert protocol._supports_delete(wrapped) is True
    assert protocol._supports_delete(MyBackend(NoDelete())) is False
    # An op the subclass defines itself is left alone.
    own = OwnDelete(NoDelete())
    assert getattr(type(own), "delete") is OwnDelete.delete


@workflow.defn
class ShellBackendWorkflow:
    @workflow.run
    async def run(self, root_dir: str) -> str:
        backend = TemporalBackend(
            LocalShellBackend(root_dir=root_dir),
            activity_options={"start_to_close_timeout": timedelta(seconds=30)},
        )
        result = await backend.aexecute("echo shell-ok")
        # The per-command timeout deepagents forwards to a timeout-capable
        # sandbox crosses the activity boundary along with the command.
        timed = await backend.aexecute("echo shell-timeout-ok", timeout=5)
        return f"{getattr(result, 'output', result)}|{getattr(timed, 'output', timed)}"


@pytest.mark.asyncio
async def test_temporal_backend_execute_runs_as_activity(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-shell-backend",
        workflows=[ShellBackendWorkflow],
        plugins=[plugin],
    ):
        handle = await env.client.start_workflow(
            ShellBackendWorkflow.run,
            str(tmp_path),
            id=f"da-shell-backend-{uuid.uuid4()}",
            task_queue="da-shell-backend",
        )
        out = await handle.result()

    assert "shell-ok" in out
    assert "shell-timeout-ok" in out
    counts = await count_scheduled_activities(handle)
    # One activity per execute call; the second carried ``timeout=5``.
    assert counts[BACKEND_OP] == 2, counts


@workflow.defn
class ShellAgentWorkflow:
    @workflow.run
    async def run(self, root_dir: str) -> str:
        backend = TemporalBackend(
            LocalShellBackend(root_dir=root_dir),
            activity_options={"start_to_close_timeout": timedelta(seconds=30)},
        )
        agent = create_deep_agent(
            model="anthropic:claude-sonnet-4-5",
            backend=backend,
            system_prompt="Run the command, then report its output.",
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Run it."}]}
        )
        # The execute tool's own message says whether deepagents ran the
        # command or declined the timeout up front.
        return "\n".join(str(m.content) for m in result["messages"] if m.type == "tool")


@pytest.mark.asyncio
async def test_agent_execute_tool_forwards_timeout_through_backend(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """deepagents' built-in ``execute`` tool, called WITH a per-command
    ``timeout``, runs through a TemporalBackend-wrapped LocalShellBackend.

    Regression: deepagents gates the timeout on
    ``execute_accepts_timeout(type(backend))``, a signature probe for a
    ``timeout`` parameter on ``execute``. The dispatcher's bare
    ``(*args, **kwargs)`` read False, so the tool answered "does not support
    per-command timeout overrides" for a backend that accepts one unwrapped.
    """
    from langchain_core.messages import AIMessage

    from temporalio.contrib.deepagents.testing import mock_model_provider

    execute_turn = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "execute",
                "args": {"command": "echo tool-timeout-ok", "timeout": 5},
                "id": "call-execute",
            }
        ],
    )
    final = AIMessage(content="Done.")
    plugin = DeepAgentsPlugin(
        model_provider=mock_model_provider([execute_turn, final]),
    )
    async with Worker(
        env.client,
        task_queue="da-shell-agent",
        workflows=[ShellAgentWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await env.client.start_workflow(
            ShellAgentWorkflow.run,
            str(tmp_path),
            id=f"da-shell-agent-{uuid.uuid4()}",
            task_queue="da-shell-agent",
        )
        out = await handle.result()

    assert "tool-timeout-ok" in out, out
    assert "does not support per-command timeout" not in out, out
    counts = await count_scheduled_activities(handle)
    # The command ran as an activity, not in the workflow.
    assert counts[BACKEND_OP] == 1, counts
    assert counts["deepagents.invoke_model"] == 2, counts


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


def test_temporal_backend_gc_keeps_ref_resolvable_for_inflight_activity() -> None:
    # A backend_op activity scheduled just before a cache eviction can start
    # AFTER the evicted wrapper is collected, and the replay that would
    # re-register the ref only happens once that activity completes. The
    # activity-side lookup must therefore still resolve a retired ref.
    inner = RecordingBackend()
    before = set(registered_backends())
    wrapper = TemporalBackend(inner)
    (ref,) = set(registered_backends()) - before
    del wrapper
    gc.collect()
    assert ref not in registered_backends()
    assert lookup_backend(ref) is inner


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


class MutatingBackend:
    """Read result depends on op order: each read returns the write count so a
    served-stale-cache regression is observable.

    State lives on DISK, like a real backend's. In-memory state (instance or
    class level) cannot work here: the workflow re-constructs and re-registers
    the fake from a FRESH sandbox on every replayed activation under
    ``max_cached_workflows=0``, so a replay landing between the write and the
    second read swaps in a new module copy with reset in-memory state.
    """

    def __init__(self, root: str) -> None:
        self._log = Path(root) / "writes.log"

    def _count(self) -> int:
        return len(self._log.read_text().splitlines()) if self._log.exists() else 0

    def write(self, _file_path: str, _content: str) -> str:
        with self._log.open("a") as f:
            f.write("w\n")
        return f"wrote:{self._count()}"

    def read(self, _file_path: str) -> str:
        return f"read-at-write-count:{self._count()}"


class _RepeatedOpsAgent:
    """ainvoke-shaped driver: read, write, read the SAME path — under
    run_deep_agent so the continue-as-new result cache is active."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def ainvoke(self, _input: Any) -> dict:
        first = await self._backend.read("a.txt")
        await self._backend.write("a.txt", "x")
        second = await self._backend.read("a.txt")
        return {"messages": [f"{first}|{second}"], "todos": []}


@workflow.defn
class RepeatedBackendOpWorkflow:
    @workflow.run
    async def run(self, input: dict, state_snapshot: dict | None = None) -> str:
        from temporalio.contrib.deepagents import run_deep_agent

        backend = TemporalBackend(
            MutatingBackend(input["root"]),
            # No retries: the disk log grows per activity ATTEMPT and the
            # assertions are exact counts.
            activity_options={
                "start_to_close_timeout": timedelta(seconds=30),
                "retry_policy": RetryPolicy(maximum_attempts=1),
            },
        )
        result = await run_deep_agent(
            _RepeatedOpsAgent(backend), input, state_snapshot=state_snapshot
        )
        return str(result["messages"][-1])


@pytest.mark.asyncio
async def test_repeated_backend_op_sees_fresh_state(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A repeated identical read after an intervening write runs its own
    Activity and sees the write — under the active CAN cache, the old
    payload-only key served the FIRST read's stale result."""
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-repeated-backend-op",
        workflows=[RepeatedBackendOpWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await env.client.start_workflow(
            RepeatedBackendOpWorkflow.run,
            {"messages": [], "root": str(tmp_path)},
            id=f"da-repeated-backend-op-{uuid.uuid4()}",
            task_queue="da-repeated-backend-op",
        )
        out = await handle.result()

    assert out == "read-at-write-count:0|read-at-write-count:1", out
    counts = await count_scheduled_activities(handle)
    assert counts[BACKEND_OP] == 3, counts
