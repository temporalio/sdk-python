"""Worker subprocess for testing OS signal delivery without signalling pytest."""

from __future__ import annotations

import asyncio
import signal
import sys
import uuid
from contextlib import AsyncExitStack
from datetime import timedelta

import temporalio.activity
import temporalio.client
import temporalio.worker
import temporalio.workflow


@temporalio.workflow.defn
class SignalWorkflow:
    @temporalio.workflow.run
    async def run(self) -> None:
        await temporalio.workflow.execute_activity(
            "signal_activity", start_to_close_timeout=timedelta(seconds=30)
        )


async def main() -> None:
    client = await temporalio.client.Client.connect(sys.argv[1], namespace=sys.argv[2])
    use_context = sys.argv[3] == "context"
    expire_grace = sys.argv[4] == "expire"
    started = [asyncio.Event(), asyncio.Event()]
    cleaned_up: set[int] = set()

    def make_activity(index: int):
        @temporalio.activity.defn(name="signal_activity")
        async def signal_activity() -> None:
            started[index].set()
            try:
                await temporalio.activity.wait_for_worker_shutdown()
                print(f"SHUTDOWN_STARTED {index}", flush=True)
                if expire_grace:
                    await asyncio.Future()
                else:
                    await asyncio.sleep(0.3)
                    print(f"ACTIVITY_COMPLETED {index}", flush=True)
            except asyncio.CancelledError:
                print(f"ACTIVITY_CANCELLED {index}", flush=True)
                raise
            finally:
                await asyncio.sleep(0.05)
                cleaned_up.add(index)
                print(f"ACTIVITY_CLEANED_UP {index}", flush=True)

        return signal_activity

    task_queues = [str(uuid.uuid4()) for _ in range(2)]
    workers = [
        temporalio.worker.Worker(
            client,
            task_queue=task_queues[i],
            workflows=[SignalWorkflow],
            activities=[make_activity(i)],
            workflow_runner=temporalio.worker.UnsandboxedWorkflowRunner(),
            graceful_shutdown_timeout=timedelta(seconds=0.5),
        )
        for i in range(2)
    ]
    tasks: list[asyncio.Task] = []
    try:
        async with AsyncExitStack() as stack:
            for worker, task_queue in zip(workers, task_queues):
                if use_context:
                    await stack.enter_async_context(worker)
                else:
                    tasks.append(asyncio.create_task(worker.run()))
                await client.start_workflow(
                    SignalWorkflow.run,
                    id=str(uuid.uuid4()),
                    task_queue=task_queue,
                )
            await asyncio.gather(*(event.wait() for event in started))
            print("READY", flush=True)
            try:
                if use_context:
                    await asyncio.Future()
                else:
                    await asyncio.gather(*tasks)
            finally:
                await asyncio.sleep(0.05)
                print("BODY_CLEANED_UP", flush=True)
    except asyncio.CancelledError:
        await asyncio.gather(*tasks, return_exceptions=True)
    assert all(worker.is_shutdown for worker in workers)
    assert cleaned_up == {0, 1}
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    print("SHUTDOWN_COMPLETE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
