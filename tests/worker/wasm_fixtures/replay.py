"""Replay histories from the mixed worker in a fresh process."""

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from .native import NativeWorkflow


async def replay(bundle: str, namespace: str, paths: list[str]) -> None:
    async def histories() -> AsyncIterator[WorkflowHistory]:
        for filename in paths:
            path = Path(filename)
            yield WorkflowHistory.from_json(path.stem, path.read_text())

    await Replayer(
        workflows=[NativeWorkflow],
        wasm_workflow_bundle=bundle,
        namespace=namespace,
    ).replay_workflows(histories())


if __name__ == "__main__":
    asyncio.run(replay(sys.argv[1], sys.argv[2], sys.argv[3:]))
