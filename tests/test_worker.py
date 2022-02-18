import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import pytest

import temporalio.activity
import temporalio.client
import temporalio.worker
from tests.fixtures import utils


async def test_activity_hello(client: temporalio.client.Client, worker: utils.Worker):
    async def say_hello(name: str) -> str:
        return f"Hello, {name}!"

    result = await _execute_workflow_with_activity(
        client, worker, say_hello, "Temporal"
    )
    assert result.result == "Hello, Temporal!"


async def test_activity_info(client: temporalio.client.Client, worker: utils.Worker):
    # Make sure info call outside of activity context fails
    assert not temporalio.activity.in_activity()
    with pytest.raises(RuntimeError) as err:
        temporalio.activity.info()
    assert err.value.args == ("Not in activity context",)

    # Capture the info from the activity
    info: Optional[temporalio.activity.Info] = None

    async def capture_info() -> None:
        nonlocal info
        info = temporalio.activity.info()

    result = await _execute_workflow_with_activity(
        client, worker, capture_info, start_to_close_timeout_ms=4000
    )

    assert info
    assert info.activity_id
    assert info.activity_type == "capture_info"
    assert info.attempt == 1
    assert info.heartbeat_details == []
    assert info.heartbeat_timeout is None
    # TODO(cretz): Broken?
    # assert info.schedule_to_close_timeout is None
    assert abs(info.scheduled_time - datetime.now(timezone.utc)) < timedelta(seconds=5)
    assert info.start_to_close_timeout == timedelta(seconds=4)
    assert abs(info.started_time - datetime.now(timezone.utc)) < timedelta(seconds=5)
    assert info.task_queue == result.act_task_queue
    assert info.workflow_id == result.handle.id
    assert info.workflow_namespace == client.namespace
    assert info.workflow_run_id == result.handle.first_execution_run_id
    assert info.workflow_type == "kitchen_sink"


# async def test_activity_failure():
#     raise NotImplementedError

# async def test_activity_cancel():
#     raise NotImplementedError

# async def test_activity_catch_cancel():
#     raise NotImplementedError

# async def test_activity_not_exist():
#     raise NotImplementedError

# async def test_max_concurrent_activities():
#     raise NotImplementedError

# async def test_activity_type_hints():
#     raise NotImplementedError

# async def test_activity_async_with_executor():
#     raise NotImplementedError

# async def test_activity_interceptor():
#     raise NotImplementedError

# async def test_activity_async_completion():
#     raise NotImplementedError

# async def test_activity_heartbeat():
#     raise NotImplementedError

# async def test_activity_retry():
#     raise NotImplementedError

# async def test_activity_non_retry_error():
#     raise NotImplementedError

# async def test_activity_logging():
#     raise NotImplementedError


@dataclass
class _ActivityResult:
    act_task_queue: str
    result: Any
    handle: temporalio.client.WorkflowHandle


async def _execute_workflow_with_activity(
    client: temporalio.client.Client,
    worker: utils.Worker,
    fn: Callable,
    *args: Any,
    start_to_close_timeout_ms: Optional[int] = None,
) -> _ActivityResult:
    act_task_queue = str(uuid.uuid4())
    async with temporalio.worker.Worker(
        client, task_queue=act_task_queue, activities={fn.__name__: fn}
    ):
        handle = await client.start_workflow(
            "kitchen_sink",
            utils.KSWorkflowParams(
                actions=[
                    utils.KSAction(
                        execute_activity=utils.KSExecuteActivityAction(
                            name=fn.__name__,
                            task_queue=act_task_queue,
                            args=args,
                            start_to_close_timeout_ms=start_to_close_timeout_ms,
                        )
                    )
                ]
            ),
            id=str(uuid.uuid4()),
            task_queue=worker.task_queue,
        )
        return _ActivityResult(
            act_task_queue=act_task_queue, result=await handle.result(), handle=handle
        )
