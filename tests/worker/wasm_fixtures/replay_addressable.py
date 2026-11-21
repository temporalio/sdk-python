"""Replay the addressable work in a process with cold host and guest caches."""

import asyncio
import sys
from pathlib import Path

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer


async def replay(bundle: str, store: str, history_path: str, namespace: str) -> None:
    path = Path(history_path)
    await Replayer(
        namespace=namespace,
        wasm_workflow_bundle=bundle,
        wasm_addressable_store=store,
    ).replay_workflow(WorkflowHistory.from_json(path.stem, path.read_text()))


if __name__ == "__main__":
    asyncio.run(replay(*sys.argv[1:]))
