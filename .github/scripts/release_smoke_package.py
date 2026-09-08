"""Smoke test an installed temporalio release package."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import temporalio
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker


@activity.defn
async def say_hello(name: str) -> str:
    return f"Hello, {name}!"


@workflow.defn
class SmokeWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello,
            name,
            start_to_close_timeout=timedelta(seconds=10),
        )


async def main() -> None:
    expected_version = os.environ["VERSION"]
    if temporalio.__version__ != expected_version:
        raise RuntimeError(
            f"Expected temporalio {expected_version}, got {temporalio.__version__}"
        )

    task_queue = f"release-smoke-{uuid.uuid4()}"
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[SmokeWorkflow],
            activities=[say_hello],
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            result = await env.client.execute_workflow(
                SmokeWorkflow.run,
                "trusted publishing",
                id=task_queue,
                task_queue=task_queue,
            )
    if result != "Hello, trusted publishing!":
        raise RuntimeError(f"Unexpected workflow result: {result!r}")


if __name__ == "__main__":
    asyncio.run(main())
