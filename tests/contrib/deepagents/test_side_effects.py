"""Determinism: no unexpected side effects and a bounded activity count.

Running the worker with ``max_cached_workflows=0`` forces the workflow to be
replayed from history on every activation. If the in-workflow dispatch did
anything nondeterministic, replay would diverge and the workflow would fail. A
clean completion plus an exact ``backend_op`` schedule count proves the seam
schedules one activity per op and nothing more.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta

import pytest

from temporalio.testing import WorkflowEnvironment

pytestmark = pytest.mark.skipif(
    sys.version_info < (3, 11), reason="deepagents requires Python >= 3.11"
)
from temporalio import workflow
from temporalio.contrib.deepagents import DeepAgentsPlugin, TemporalBackend
from temporalio.worker import Worker
from tests.contrib.deepagents.helpers import count_scheduled_activities

BACKEND_OP = "deepagents.backend_op"


class TwoOpBackend:
    """Protocol-named ops: one async (``awrite``, as the filesystem
    middleware calls it) and one sync (``read``)."""

    async def awrite(self, file_path: str, _content: str) -> str:
        return f"wrote:{file_path}"

    def read(self, file_path: str) -> str:
        return f"read:{file_path}"


@workflow.defn
class TwoOpWorkflow:
    @workflow.run
    async def run(self) -> str:
        backend = TemporalBackend(
            TwoOpBackend(),
            activity_options={"start_to_close_timeout": timedelta(seconds=10)},
        )
        await backend.awrite("a.txt", "hello")
        return await backend.read("a.txt")


@pytest.mark.asyncio
async def test_activity_schedule_counts(env: WorkflowEnvironment) -> None:
    plugin = DeepAgentsPlugin()
    async with Worker(
        env.client,
        task_queue="da-side-effects",
        workflows=[TwoOpWorkflow],
        plugins=[plugin],
        max_cached_workflows=0,
    ):
        handle = await env.client.start_workflow(
            TwoOpWorkflow.run,
            id=f"da-side-effects-{uuid.uuid4()}",
            task_queue="da-side-effects",
        )
        out = await handle.result()

    assert out == "read:a.txt"
    counts = await count_scheduled_activities(handle)
    # Exactly the two backend ops, each scheduled once — no hidden replays of work.
    assert counts[BACKEND_OP] == 2, counts
