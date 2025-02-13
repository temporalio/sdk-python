import asyncio
import socket
import time
import uuid
from contextlib import closing
from datetime import timedelta
from typing import Awaitable, Callable, Optional, Sequence, Type, TypeVar

from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import (
    AddSearchAttributesRequest,
    ListSearchAttributesRequest,
)
from temporalio.api.update.v1 import UpdateRef
from temporalio.api.workflowservice.v1 import PollWorkflowExecutionUpdateRequest
from temporalio.client import BuildIdOpAddNewDefault, Client, WorkflowHandle
from temporalio.common import SearchAttributeKey
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.workflow import UpdateMethodMultiParam


def new_worker(
    client: Client,
    *workflows: Type,
    activities: Sequence[Callable] = [],
    task_queue: Optional[str] = None,
    workflow_runner: WorkflowRunner = SandboxedWorkflowRunner(),
    max_cached_workflows: int = 1000,
    workflow_failure_exception_types: Sequence[Type[BaseException]] = [],
    **kwargs,
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue or str(uuid.uuid4()),
        workflows=workflows,
        activities=activities,
        workflow_runner=workflow_runner,
        max_cached_workflows=max_cached_workflows,
        workflow_failure_exception_types=workflow_failure_exception_types,
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
    ), f"timed out waiting for equal, asserted against last value of {last_value}"


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


async def ensure_search_attributes_present(
    client: Client, *keys: SearchAttributeKey
) -> None:
    """Ensure all search attributes are present or attempt to add all."""
    # Add search attributes if not already present
    resp = await client.operator_service.list_search_attributes(
        ListSearchAttributesRequest(namespace=client.namespace)
    )
    if not set(key.name for key in keys).issubset(resp.custom_attributes.keys()):
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
        resp = await client.operator_service.list_search_attributes(
            ListSearchAttributesRequest(namespace=client.namespace)
        )
        assert set(key.name for key in keys).issubset(resp.custom_attributes.keys())


def find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


async def workflow_update_exists(
    client: Client, workflow_id: str, update_id: str
) -> bool:
    try:
        await client.workflow_service.poll_workflow_execution_update(
            PollWorkflowExecutionUpdateRequest(
                namespace=client.namespace,
                update_ref=UpdateRef(
                    workflow_execution=WorkflowExecution(workflow_id=workflow_id),
                    update_id=update_id,
                ),
            )
        )
        return True
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise
        return False


# TODO: type update return value
async def admitted_update_task(
    client: Client,
    handle: WorkflowHandle,
    update_method: UpdateMethodMultiParam,
    id: str,
    **kwargs,
) -> asyncio.Task:
    """
    Return an asyncio.Task for an update after waiting for it to be admitted.
    """
    update_task = asyncio.create_task(
        handle.execute_update(update_method, id=id, **kwargs)
    )
    await assert_eq_eventually(
        True,
        lambda: workflow_update_exists(client, handle.id, id),
    )
    return update_task
