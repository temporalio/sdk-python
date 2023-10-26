import asyncio
import socket
import time
import uuid
from contextlib import closing
from datetime import timedelta
from typing import Awaitable, Callable, Optional, Sequence, Type, TypeVar

from temporalio.client import BuildIdOpAddNewDefault, Client
from temporalio.service import RPCError, RPCStatusCode
from temporalio.common import SearchAttributeKey
from temporalio.worker import Worker, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import ListSearchAttributesRequest, AddSearchAttributesRequest


def new_worker(
    client: Client,
    *workflows: Type,
    activities: Sequence[Callable] = [],
    task_queue: Optional[str] = None,
    workflow_runner: WorkflowRunner = SandboxedWorkflowRunner(),
    max_cached_workflows: int = 1000,
    **kwargs,
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue or str(uuid.uuid4()),
        workflows=workflows,
        activities=activities,
        workflow_runner=workflow_runner,
        max_cached_workflows=max_cached_workflows,
        **kwargs,
    )


T = TypeVar("T")


async def assert_eq_eventually(
    expected: T,
    fn: Callable[[], Awaitable[T]],
    *,
    timeout: timedelta = timedelta(seconds=10),
    interval: timedelta = timedelta(milliseconds=200),
) -> None:
    start_sec = time.monotonic()
    last_value = None
    while timedelta(seconds=time.monotonic() - start_sec) < timeout:
        last_value = await fn()
        if expected == last_value:
            return
        await asyncio.sleep(interval.total_seconds())
    assert (
        expected == last_value
    ), "timed out waiting for equal, asserted against last value"


async def worker_versioning_enabled(client: Client) -> bool:
    tq = f"worker-versioning-init-test-{uuid.uuid4()}"
    try:
        await client.update_worker_build_id_compatibility(
            tq, BuildIdOpAddNewDefault("testver")
        )
        return True
    except RPCError as e:
        if e.status in [RPCStatusCode.PERMISSION_DENIED, RPCStatusCode.UNIMPLEMENTED]:
            return False
        raise

async def ensure_search_attributes_present(client: Client, *keys: SearchAttributeKey) -> None:
    """Ensure all search attributes are present or attempt to add all."""
    async def search_attributes_present() -> bool:
        resp = await client.operator_service.list_search_attributes(
            ListSearchAttributesRequest(namespace=client.namespace)
        )
        return sorted(resp.custom_attributes.keys()) == sorted([key.name for key in keys])

    # Add search attributes if not already present
    if not await search_attributes_present():
        await client.operator_service.add_search_attributes(
            AddSearchAttributesRequest(
                namespace=client.namespace,
                search_attributes={
                    key.name: IndexedValueType.ValueType(key.indexed_value_type)
                    for key in keys
                },
            ),
        )
    # Confirm now present
    assert await search_attributes_present()

def find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]
