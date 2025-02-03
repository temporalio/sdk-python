import uuid
from datetime import datetime, timedelta
from ipaddress import IPv4Address
from typing import Annotated, Any, List, Sequence, TypeVar

from annotated_types import Len
from pydantic import BaseModel, Field, WithJsonSchema

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pydantic.converter import pydantic_data_converter
from temporalio.worker import Worker

SequenceType = TypeVar("SequenceType", bound=Sequence[Any])
ShortSequence = Annotated[SequenceType, Len(max_length=2)]


class MyPydanticModel(BaseModel):
    ip_field: IPv4Address
    datetime_field: datetime
    string_field_assigned_field: str = Field()
    datetime_field_assigned_field: datetime = Field()
    string_field_with_default: str = Field(default_factory=lambda: "my-string")
    datetime_field_with_default: datetime = Field(
        default_factory=lambda: datetime(2000, 1, 2, 3, 4, 5)
    )
    annotated_datetime: Annotated[datetime, Field(), WithJsonSchema({"extra": "data"})]
    annotated_list_of_str: Annotated[
        List[str], Field(), WithJsonSchema({"extra": "data"})
    ]
    annotated_list_of_datetime: Annotated[
        List[datetime], Field(), WithJsonSchema({"extra": "data"})
    ]
    str_short_sequence: ShortSequence[List[str]]
    datetime_short_sequence: ShortSequence[List[datetime]]


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
            ip_field=IPv4Address("127.0.0.1"),
            datetime_field=datetime(2000, 1, 2, 3, 4, 5),
            string_field_assigned_field="my-string",
            datetime_field_assigned_field=datetime(2000, 1, 2, 3, 4, 5),
            annotated_datetime=datetime(2000, 1, 2, 3, 4, 5),
            annotated_list_of_str=["my-string-1", "my-string-2"],
            annotated_list_of_datetime=[
                datetime(2000, 1, 2, 3, 4, 5),
                datetime(2000, 11, 12, 13, 14, 15),
            ],
            str_short_sequence=["my-string-1", "my-string-2"],
            datetime_short_sequence=[
                datetime(2000, 1, 2, 3, 4, 5),
                datetime(2000, 11, 12, 13, 14, 15),
            ],
        ),
        MyPydanticModel(
            ip_field=IPv4Address("127.0.0.2"),
            datetime_field=datetime(2001, 2, 3, 4, 5, 6),
            string_field_assigned_field="my-string",
            datetime_field_assigned_field=datetime(2000, 2, 3, 4, 5, 6),
            annotated_datetime=datetime(2001, 2, 3, 4, 5, 6),
            annotated_list_of_str=["my-string-3", "my-string-4"],
            annotated_list_of_datetime=[
                datetime(2001, 2, 3, 4, 5, 6),
                datetime(2001, 12, 13, 14, 15, 16),
            ],
            str_short_sequence=["my-string-3", "my-string-4"],
            datetime_short_sequence=[
                datetime(2001, 2, 3, 4, 5, 6),
                datetime(2001, 12, 13, 14, 15, 16),
            ],
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
