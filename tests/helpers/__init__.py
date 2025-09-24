import asyncio
import logging
import socket
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Sequence, Type, TypeVar, Union

from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.enums.v1 import EventType as EventType
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.history.v1 import HistoryEvent
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
from temporalio.converter import DataConverter
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker, WorkflowRunner
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.workflow import (
    UpdateMethodMultiParam,
)

logger = logging.getLogger(__name__)


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
        logger.info("Checking workflow update status: %s", update_id)
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


async def print_history(handle: WorkflowHandle):
    i = 1
    async for evt in handle.fetch_history_events():
        event = EventType.Name(evt.event_type).removeprefix("EVENT_TYPE_")
        print(f"{i:2}: {event}")
        i += 1


@dataclass
class InterleavedHistoryEvent:
    handle: WorkflowHandle
    event: Union[HistoryEvent, str]
    number: Optional[int]
    time: datetime


async def print_interleaved_histories(
    handles: list[WorkflowHandle],
    extra_events: Optional[list[tuple[WorkflowHandle, str, datetime]]] = None,
) -> None:
    """
    Print the interleaved history events from multiple workflow handles in columns.

    A column entry looks like

    <event_num>: <elapsed_ms> <event_type>

    where <elapsed_ms> is the number of milliseconds since the first event in any of the workflows.
    """
    all_events: list[InterleavedHistoryEvent] = []
    workflow_start_times: dict[WorkflowHandle, datetime] = {}

    for handle in handles:
        event_num = 1
        first_event = True
        async for history_event in handle.fetch_history_events():
            event_time = history_event.event_time.ToDatetime()
            if first_event:
                workflow_start_times[handle] = event_time
                first_event = False
            all_events.append(
                InterleavedHistoryEvent(handle, history_event, event_num, event_time)
            )
            event_num += 1

    if extra_events:
        for handle, event_str, event_time in extra_events:
            # Ensure timezone-naive
            if event_time.tzinfo is not None:
                event_time = event_time.astimezone(timezone.utc).replace(tzinfo=None)
            all_events.append(
                InterleavedHistoryEvent(handle, event_str, None, event_time)
            )

    zero_time = min(workflow_start_times.values())

    all_events.sort(key=lambda item: item.time)
    col_width = 50

    def _format_row(items: list[str], truncate: bool = False) -> str:
        if truncate:
            items = [item[: col_width - 3] for item in items]
        return " | ".join(f"{item:<{col_width - 3}}" for item in items)

    headers = [handle.id for handle in handles]
    print("\n" + _format_row(headers, truncate=True))
    print("-" * (col_width * len(handles) + len(handles) - 1))

    for event in all_events:
        elapsed_ms = int((event.time - zero_time).total_seconds() * 1000)

        if isinstance(event.event, str):
            event_desc = f" *: {elapsed_ms:>4} {event.event}"
            summary = None
        else:
            event_type = EventType.Name(event.event.event_type).removeprefix(
                "EVENT_TYPE_"
            )
            event_desc = f"{event.number:2}: {elapsed_ms:>4} {event_type}"

            # Extract summary from user_metadata if present
            summary = None
            if event.event.HasField(
                "user_metadata"
            ) and event.event.user_metadata.HasField("summary"):
                try:
                    summary = DataConverter.default.payload_converter.from_payload(
                        event.event.user_metadata.summary
                    )
                except Exception:
                    pass  # Ignore decoding errors

        row = [""] * len(handles)
        col_idx = handles.index(event.handle)
        row[col_idx] = event_desc[: col_width - 3]
        print(_format_row(row))

        # Print summary on new line if present
        if summary:
            summary_row = [""] * len(handles)
            # Left-align with event type name (after "<event_num>: <elapsed_ms> ")
            # Calculate the padding needed
            if event.number is not None:
                padding = len(f"{event.number:2}: {elapsed_ms:>4} ")
            else:
                padding = len(f" *: {elapsed_ms:>4} ")
            summary_row[col_idx] = f"{' ' * padding}[{summary}]"[: col_width - 3]
            print(_format_row(summary_row))
