import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncIterator

from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

assert sys.version_info >= (3, 9)


@workflow.defn
class BenchWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            bench_activity, name, start_to_close_timeout=timedelta(seconds=30)
        )


@activity.defn
async def bench_activity(name: str) -> str:
    return f"Hello, {name}!"


async def main():
    logging.basicConfig(
        format="%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s",
        level=logging.WARN,
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger = logging.getLogger(__name__)
    max_mem = -1

    parser = argparse.ArgumentParser(description="Run bench")
    parser.add_argument("--workflow-count", type=int, required=True)
    parser.add_argument("--sandbox", action=argparse.BooleanOptionalAction)
    parser.add_argument("--max-cached-workflows", type=int, required=True)
    parser.add_argument("--max-concurrent", type=int, required=True)
    args = parser.parse_args()

    @asynccontextmanager
    async def track_mem() -> AsyncIterator[None]:
        # We intentionally import in here so the sandbox doesn't grow huge with
        # this import
        import psutil

        # Get mem every 800ms
        process = psutil.Process()

        async def report_mem():
            nonlocal max_mem
            while True:
                try:
                    await asyncio.sleep(0.8)
                finally:
                    # TODO(cretz): "vms" appears more accurate on Windows, but
                    # rss is more accurate on Linux
                    used_mem = process.memory_info().rss
                    if used_mem > max_mem:
                        max_mem = used_mem

        report_mem_task = asyncio.create_task(report_mem())
        try:
            yield None
        finally:
            report_mem_task.cancel()

    logger.info("Running %s workflows", args.workflow_count)
    async with track_mem():
        # Run with a local workflow environment
        logger.debug("Starting local environment")
        async with await WorkflowEnvironment.start_local() as env:
            task_queue = f"task-queue-{uuid.uuid4()}"

            # Create a bunch of workflows
            logger.debug("Starting %s workflows", args.workflow_count)
            pre_start_seconds = time.monotonic()
            handles = [
                await env.client.start_workflow(
                    BenchWorkflow.run,
                    f"user-{i}",
                    id=f"workflow-{i}-{uuid.uuid4()}",
                    task_queue=task_queue,
                )
                for i in range(args.workflow_count)
            ]
            start_seconds = time.monotonic() - pre_start_seconds

            # Start a worker to run them
            logger.debug("Starting worker")
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[BenchWorkflow],
                activities=[bench_activity],
                workflow_runner=SandboxedWorkflowRunner()
                if args.sandbox
                else UnsandboxedWorkflowRunner(),
                max_cached_workflows=args.max_cached_workflows,
                max_concurrent_workflow_tasks=args.max_concurrent,
                max_concurrent_activities=args.max_concurrent,
            ):
                logger.debug("Worker started")
                # Wait for them all
                pre_result_seconds = time.monotonic()
                for h in handles:
                    await h.result()
                result_seconds = time.monotonic() - pre_result_seconds
                logger.debug("All workflows complete")

    # Print results
    json.dump(
        {
            "workflow_count": args.workflow_count,
            "sandbox": args.sandbox or False,
            "max_cached_workflows": args.max_cached_workflows,
            "max_concurrent": args.max_concurrent,
            "max_mem_mib": round(max_mem / 1024**2, 1),
            "start_seconds": round(start_seconds, 1),
            "result_seconds": round(result_seconds, 1),
            "workflows_per_second": round(args.workflow_count / result_seconds, 1),
        },
        sys.stdout,
        indent=2,
    )


if __name__ == "__main__":
    asyncio.run(main())
