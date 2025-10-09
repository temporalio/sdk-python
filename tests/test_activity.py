import uuid
from datetime import timedelta

from temporalio import activity
from temporalio.client import Client
from temporalio.common import ActivityExecutionStatus


@activity.defn
async def increment(input: int) -> int:
    return input + 1


async def test_describe_activity(client: Client):
    activity_id = str("test_start_and_describe_activity_id")
    task_queue = str(uuid.uuid4())

    activity_handle = await client.start_activity(
        increment,
        args=(1,),
        id=activity_id,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=5),
    )
    desc = await activity_handle.describe()
    assert desc.activity_id == activity_id
    # TODO: server not returning run ID yet
    # assert desc.run_id == activity_handle.run_id
    assert desc.activity_type == "increment"
    assert desc.task_queue == task_queue
    assert desc.status == ActivityExecutionStatus.RUNNING
