import dataclasses
import uuid
from datetime import datetime, timedelta
from ipaddress import IPv4Address
from typing import List

from pydantic import BaseModel

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pydantic.converter import pydantic_data_converter
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)


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


# Due to known issues with Pydantic's use of issubclass and our inability to
# override the check in sandbox, Pydantic will think datetime is actually date
# in the sandbox. At the expense of protecting against datetime.now() use in
# workflows, we're going to remove datetime module restrictions. See sdk-python
# README's discussion of known sandbox issues for more details.
def new_sandbox_runner() -> SandboxedWorkflowRunner:
    # TODO(cretz): Use with_child_unrestricted when https://github.com/temporalio/sdk-python/issues/254
    # is fixed and released
    invalid_module_member_children = dict(
        SandboxRestrictions.invalid_module_members_default.children
    )
    del invalid_module_member_children["datetime"]
    return SandboxedWorkflowRunner(
        restrictions=dataclasses.replace(
            SandboxRestrictions.default,
            invalid_module_members=dataclasses.replace(
                SandboxRestrictions.invalid_module_members_default,
                children=invalid_module_member_children,
            ),
        )
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
        workflow_runner=new_sandbox_runner(),
    ):
        result = await client.execute_workflow(
            MyWorkflow.run,
            orig_models,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert orig_models == result
