import asyncio
import socket
import time
import uuid
from contextlib import closing
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional, Sequence, Type, TypeVar

from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import (
    AddSearchAttributesRequest,
    ListSearchAttributesRequest,
)
from temporalio.api.update.v1 import UpdateRef
from temporalio.api.workflow.v1 import PendingActivityInfo
from temporalio.api.workflowservice.v1 import (
    PauseActivityRequest,
    PollWorkflowExecutionUpdateRequest,
    UnpauseActivityRequest,
)
from temporalio.client import BuildIdOpAddNewDefault, Client, WorkflowHandle
from temporalio.common import SearchAttributeKey
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.workflow import (
    UpdateMethodMultiParam,
)


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


async def assert_eventually(
    fn: Callable[[], Awaitable[T]],
    *,
    timeout: timedelta = timedelta(seconds=10),
    interval: timedelta = timedelta(milliseconds=200),
) -> T:
    start_sec = time.monotonic()
    while True:
        try:
            res = await fn()
            return res
        except AssertionError:
            if timedelta(seconds=time.monotonic() - start_sec) >= timeout:
                raise
        await asyncio.sleep(interval.total_seconds())


async def assert_eq_eventually(
    expected: T,
    fn: Callable[[], Awaitable[T]],
    *,
    timeout: timedelta = timedelta(seconds=10),
    interval: timedelta = timedelta(milliseconds=200),
) -> None:
    async def check() -> None:
        assert expected == await fn()

    await assert_eventually(check, timeout=timeout, interval=interval)


async def assert_task_fail_eventually(
    handle: WorkflowHandle, *, message_contains: Optional[str] = None
) -> None:
    async def check() -> None:
        async for evt in handle.fetch_history_events():
            if evt.HasField("workflow_task_failed_event_attributes") and (
                not message_contains
                or message_contains
                in evt.workflow_task_failed_event_attributes.failure.message
            ):
                return
        assert False, "Task failure not present"

    await assert_eventually(check)


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


async def assert_workflow_exists_eventually(
    client: Client,
    workflow: Any,
    workflow_id: str,
) -> WorkflowHandle:
    handle = None

    async def check_workflow_exists() -> bool:
        nonlocal handle
        try:
            handle = client.get_workflow_handle_for(
                workflow,
                workflow_id=workflow_id,
            )
            await handle.describe()
            return True
        except RPCError as err:
            # Ignore not-found or failed precondition because child may
            # not have started yet
            if (
                err.status == RPCStatusCode.NOT_FOUND
                or err.status == RPCStatusCode.FAILED_PRECONDITION
            ):
                return False
            raise

    await assert_eq_eventually(True, check_workflow_exists)
    assert handle is not None
    return handle


async def assert_pending_activity_exists_eventually(
    handle: WorkflowHandle,
    activity_id: str,
    timeout: timedelta = timedelta(seconds=5),
) -> PendingActivityInfo:
    """Wait until a pending activity with the given ID exists and return it."""

    async def check() -> PendingActivityInfo:
        act_info = await get_pending_activity_info(handle, activity_id)
        if act_info is not None:
            return act_info
        raise AssertionError(
            f"Activity with ID {activity_id} not found in pending activities"
        )

    return await assert_eventually(check, timeout=timeout)


async def get_pending_activity_info(
    handle: WorkflowHandle,
    activity_id: str,
) -> Optional[PendingActivityInfo]:
    """Get pending activity info by ID, or None if not found."""
    desc = await handle.describe()
    for act in desc.raw_description.pending_activities:
        if act.activity_id == activity_id:
            return act
    return None


async def pause_and_assert(client: Client, handle: WorkflowHandle, activity_id: str):
    """Pause the given activity and assert it becomes paused."""
    desc = await handle.describe()
    req = PauseActivityRequest(
        namespace=client.namespace,
        execution=WorkflowExecution(
            workflow_id=desc.raw_description.workflow_execution_info.execution.workflow_id,
            run_id=desc.raw_description.workflow_execution_info.execution.run_id,
        ),
        id=activity_id,
    )
    await client.workflow_service.pause_activity(req)

    # Assert eventually paused
    async def check_paused() -> bool:
        info = await assert_pending_activity_exists_eventually(handle, activity_id)
        return info.paused

    await assert_eventually(check_paused)


async def unpause_and_assert(client: Client, handle: WorkflowHandle, activity_id: str):
    """Unpause the given activity and assert it is not paused."""
    desc = await handle.describe()
    req = UnpauseActivityRequest(
        namespace=client.namespace,
        execution=WorkflowExecution(
            workflow_id=desc.raw_description.workflow_execution_info.execution.workflow_id,
            run_id=desc.raw_description.workflow_execution_info.execution.run_id,
        ),
        id=activity_id,
    )
    await client.workflow_service.unpause_activity(req)

    # Assert eventually not paused
    async def check_unpaused() -> bool:
        info = await assert_pending_activity_exists_eventually(handle, activity_id)
        return not info.paused

    await assert_eventually(check_unpaused)
