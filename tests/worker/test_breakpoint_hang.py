from __future__ import annotations

import pdb
import threading
import uuid
from types import FrameType
from typing import Any
from unittest.mock import patch

import pytest

from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox._restrictions import (
    RestrictedWorkflowAccessError,
)


@workflow.defn(sandboxed=False)
class ThreadCaptureWorkflow:
    """Returns the name of the thread the workflow runs on.

    `sandboxed=False` so `threading.current_thread()` isn't intercepted —
    these tests are about thread placement, not sandbox behavior.
    """

    @workflow.run
    async def run(self) -> str:
        return threading.current_thread().name


async def test_workflow_runs_on_pool_thread_without_debug_mode(client: Client):
    """Production behavior unchanged: workflows run on `temporal_workflow_*`."""
    task_queue = f"tq-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ThreadCaptureWorkflow],
    ):
        thread_name = await client.execute_workflow(
            ThreadCaptureWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=task_queue,
        )

    main_name = threading.main_thread().name
    assert thread_name != main_name, (
        f"workflow ran on the main thread ({main_name!r}) — production behavior changed"
    )
    assert thread_name.startswith("temporal_workflow_"), (
        f"expected pool thread, got {thread_name!r}"
    )


async def test_workflow_runs_on_main_thread_in_debug_mode(client: Client):
    """debug_mode=True moves workflow activation to the asyncio main thread
    so pdb's input() reaches the controlling TTY."""
    task_queue = f"tq-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ThreadCaptureWorkflow],
        debug_mode=True,
    ):
        thread_name = await client.execute_workflow(
            ThreadCaptureWorkflow.run,
            id=f"wf-{uuid.uuid4()}",
            task_queue=task_queue,
        )

    main_name = threading.main_thread().name
    assert thread_name == main_name, (
        f"expected workflow on main thread ({main_name!r}) in debug mode; "
        f"got {thread_name!r}"
    )


@workflow.defn
class SandboxedBreakpointWorkflow:
    """Sandboxed workflow that calls breakpoint() — verifies the fix works
    without requiring users to switch to UnsandboxedWorkflowRunner."""

    @workflow.run
    async def run(self) -> str:
        bird = "chicken"
        breakpoint()
        return f"bird was {bird}"


async def test_breakpoint_works_in_sandboxed_workflow_in_debug_mode(client: Client):
    """breakpoint() inside a sandboxed workflow reaches the debugger when
    debug_mode=True — no need to switch to UnsandboxedWorkflowRunner.

    Patches `pdb.Pdb.set_trace` with a stub so CI doesn't hang on an
    interactive prompt. Reaching the stub on `MainThread` with the
    workflow's `run` frame proves the full path (sandbox relaxation ->
    our hook -> pdb) works through the sandbox. Also verifies workflow
    locals (`bird`) are visible in the captured frame.
    """
    captured: dict[str, object] = {}

    def stub_set_trace(_self: pdb.Pdb, frame: FrameType | None = None) -> None:
        captured["thread"] = threading.current_thread().name
        captured["frame_name"] = frame.f_code.co_name if frame else None
        captured["bird"] = frame.f_locals.get("bird") if frame else None
        captured["called"] = True

    task_queue = f"tq-{uuid.uuid4()}"
    with patch.object(pdb.Pdb, "set_trace", stub_set_trace):
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SandboxedBreakpointWorkflow],
            debug_mode=True,
        ):
            result = await client.execute_workflow(
                SandboxedBreakpointWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    assert result == "bird was chicken", (
        f"workflow did not complete; breakpoint() likely raised inside the sandbox: "
        f"result={result!r}"
    )
    assert captured.get("called"), "pdb.Pdb.set_trace was never reached"
    assert captured["thread"] == threading.main_thread().name, (
        f"breakpoint landed on {captured['thread']!r}, not the main thread"
    )
    assert captured["frame_name"] == "run", (
        f"breakpoint stopped at frame {captured['frame_name']!r}, "
        f"expected the workflow's `run` method"
    )
    assert captured["bird"] == "chicken", (
        f"workflow local `bird` not visible in pdb frame: got {captured['bird']!r}"
    )


async def test_breakpoint_quit_continues_workflow_in_debug_mode(client: Client):
    """Typing `q` (or hitting Ctrl-D) in a workflow pdb session should
    continue the workflow rather than failing the workflow task with
    BdbQuit. The hook overrides `do_quit`/`do_EOF` to call `do_continue`
    instead, so a debug session ends cleanly.

    Drives pdb via `cmdqueue` so no real stdin is needed. The first
    iteration of cmdloop sees `q`, which dispatches to our overridden
    `do_quit` -> `do_continue`. The workflow then completes normally.
    """
    captured: dict[str, object] = {}

    class _AutoQuitPdb(pdb.Pdb):
        """Pdb subclass that pre-queues `q` and captures frame state on
        entry to `interaction`."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.cmdqueue = ["q"]

        def interaction(  # type: ignore[override]
            self, frame: FrameType | None, traceback: Any
        ) -> Any:
            if frame is not None:
                captured["frame_name"] = frame.f_code.co_name
                captured["bird"] = frame.f_locals.get("bird")
            return super().interaction(frame, traceback)

    task_queue = f"tq-{uuid.uuid4()}"
    with patch("pdb.Pdb", _AutoQuitPdb):
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SandboxedBreakpointWorkflow],
            debug_mode=True,
        ):
            result = await client.execute_workflow(
                SandboxedBreakpointWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    assert result == "bird was chicken", (
        f"workflow did not complete after `q`; `BdbQuit` likely propagated: "
        f"result={result!r}"
    )
    assert captured.get("frame_name") == "run", (
        f"pdb didn't stop in workflow.run frame: got {captured.get('frame_name')!r}"
    )
    assert captured.get("bird") == "chicken", (
        f"workflow local `bird` not visible at pdb breakpoint: "
        f"got {captured.get('bird')!r}"
    )


async def test_sandboxed_breakpoint_points_at_debug_mode(client: Client):
    """Without `debug_mode`, calling `breakpoint()` in a sandboxed workflow
    should raise the sandbox's restricted-access error with a message that
    directs the user at `debug_mode=True` (rather than the generic
    pass-through advice that doesn't apply here).

    `workflow_failure_exception_types=[RestrictedWorkflowAccessError]` makes
    the sandbox error terminal for the workflow execution instead of
    triggering Temporal's normal task-retry loop, so the test gets the
    failure surfaced promptly without waiting on a timeout.
    """
    task_queue = f"tq-{uuid.uuid4()}"
    with pytest.raises(WorkflowFailureError) as exc_info:
        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[SandboxedBreakpointWorkflow],
            workflow_failure_exception_types=[RestrictedWorkflowAccessError],
        ):
            await client.execute_workflow(
                SandboxedBreakpointWorkflow.run,
                id=f"wf-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    cause_msg = str(exc_info.value.cause)
    assert "debug_mode=True" in cause_msg, (
        f"sandbox error didn't point at debug_mode: {cause_msg!r}"
    )
