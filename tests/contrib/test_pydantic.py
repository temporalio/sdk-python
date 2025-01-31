import uuid
from datetime import datetime, timedelta
from ipaddress import IPv4Address
from typing import List

from pydantic import BaseModel

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pydantic.converter import pydantic_data_converter
from temporalio.worker import Worker


class MyPydanticModel(BaseModel):
    some_ip: IPv4Address
    some_date: datetime


@activity.defn
async def my_activity(models: List[MyPydanticModel]) -> List[MyPydanticModel]:
    activity.logger.info("Got models in activity: %s" % models)
    return models


@workflow.defn
class MyWorkflow:
    @workflow.run
    async def run(self, models: List[MyPydanticModel]) -> List[MyPydanticModel]:
        workflow.logger.info("Got models in workflow: %s" % models)
        return await workflow.execute_activity(
            my_activity, models, start_to_close_timeout=timedelta(minutes=1)
        )


async def test_workflow_with_pydantic_model(client: Client):
    # Replace data converter in client
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_models = [
        MyPydanticModel(
            some_ip=IPv4Address("127.0.0.1"),
            some_date=datetime(2000, 1, 2, 3, 4, 5),
        ),
        MyPydanticModel(
            some_ip=IPv4Address("127.0.0.2"),
            some_date=datetime(2001, 2, 3, 4, 5, 6),
        ),
    ]

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[MyWorkflow],
        activities=[my_activity],
    ):
        result = await client.execute_workflow(
            MyWorkflow.run,
            orig_models,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert orig_models == result
