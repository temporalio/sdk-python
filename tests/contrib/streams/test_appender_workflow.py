from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.streams import BufferedAppender
from temporalio.nexus.system.stream_service import StreamRef
from temporalio.nexus.system.stream_service.workflow import get_stream
from tests.helpers import new_worker
from tests.helpers.streams import DictStreamStore
from tests.nexus.test_stream_store import _with_store


@workflow.defn
class AppenderWorkflow:
    @workflow.run
    async def run(self, stream_id: str) -> str:
        stream = get_stream(StreamRef(id=stream_id), body_type=str)
        try:
            BufferedAppender(stream)  # type: ignore[arg-type]
        except RuntimeError as err:
            return str(err)
        return "built"


async def _run(
    client: Client, workflow_type: type[Any], stream_id: str
) -> tuple[WorkflowHandle[Any, Any], Any]:
    async with new_worker(client, workflow_type) as worker:
        handle: WorkflowHandle[Any, Any] = await client.start_workflow(
            workflow_type.run,
            stream_id,
            id=f"workflow-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=30),
        )
        return handle, await handle.result()


async def test_workflow_code_cannot_build_an_appender(client: Client):
    _, message = await _run(
        _with_store(client, DictStreamStore()),
        AppenderWorkflow,
        f"stream-{uuid.uuid4()}",
    )
    assert message == (
        "BufferedAppender is for client and Activity code. In Workflow code, pass the "
        "Records to one append call."
    )
