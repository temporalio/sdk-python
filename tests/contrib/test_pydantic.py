import dataclasses
import uuid
from datetime import datetime, timedelta
from ipaddress import IPv4Address
from typing import Annotated, Any, List, Sequence, Tuple, TypeVar, Union

from annotated_types import Len
from pydantic import BaseModel, Field, WithJsonSchema

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

SequenceType = TypeVar("SequenceType", bound=Sequence[Any])
ShortSequence = Annotated[SequenceType, Len(max_length=2)]


class MyPydanticModel(BaseModel):
    ip_field: IPv4Address
    string_field_assigned_field: str = Field()
    string_field_with_default: str = Field(default_factory=lambda: "my-string")
    annotated_list_of_str: Annotated[
        List[str], Field(), WithJsonSchema({"extra": "data"})
    ]
    str_short_sequence: ShortSequence[List[str]]

    def _make_assertions(self):
        assert isinstance(self.ip_field, IPv4Address)
        assert isinstance(self.string_field_assigned_field, str)
        assert isinstance(self.string_field_with_default, str)
        assert isinstance(self.annotated_list_of_str, list)
        assert isinstance(self.str_short_sequence, list)
        assert self.annotated_list_of_str == ["my-string-1", "my-string-2"]
        assert self.str_short_sequence == ["my-string-1", "my-string-2"]


class MyPydanticDatetimeModel(BaseModel):
    datetime_field: datetime
    datetime_field_assigned_field: datetime = Field()
    datetime_field_with_default: datetime = Field(
        default_factory=lambda: datetime(2000, 1, 2, 3, 4, 5)
    )
    annotated_datetime: Annotated[datetime, Field(), WithJsonSchema({"extra": "data"})]
    annotated_list_of_datetime: Annotated[
        List[datetime], Field(), WithJsonSchema({"extra": "data"})
    ]
    datetime_short_sequence: ShortSequence[List[datetime]]

    def _make_assertions(self):
        _assert_datetime_validity(self.datetime_field)
        _assert_datetime_validity(self.datetime_field_assigned_field)
        _assert_datetime_validity(self.datetime_field_with_default)
        _assert_datetime_validity(self.annotated_datetime)
        assert isinstance(self.annotated_list_of_datetime, list)
        assert isinstance(self.datetime_short_sequence, list)
        assert self.annotated_datetime == datetime(2000, 1, 2, 3, 4, 5)
        assert self.annotated_list_of_datetime == [
            datetime(2000, 1, 2, 3, 4, 5),
            datetime(2000, 11, 12, 13, 14, 15),
        ]
        assert self.datetime_short_sequence == [
            datetime(2000, 1, 2, 3, 4, 5),
            datetime(2000, 11, 12, 13, 14, 15),
        ]


def _assert_datetime_validity(dt: datetime):
    assert isinstance(dt, datetime)
    assert issubclass(dt.__class__, datetime)


def make_pydantic_objects() -> List[Union[MyPydanticModel, MyPydanticDatetimeModel]]:
    return [
        MyPydanticModel(
            ip_field=IPv4Address("127.0.0.1"),
            string_field_assigned_field="my-string",
            annotated_list_of_str=["my-string-1", "my-string-2"],
            str_short_sequence=["my-string-1", "my-string-2"],
        ),
        MyPydanticDatetimeModel(
            datetime_field=datetime(2001, 2, 3, 4, 5, 6),
            datetime_field_assigned_field=datetime(2000, 2, 3, 4, 5, 6),
            annotated_datetime=datetime(2001, 2, 3, 4, 5, 6),
            annotated_list_of_datetime=[
                datetime(2001, 2, 3, 4, 5, 6),
                datetime(2001, 12, 13, 14, 15, 16),
            ],
            datetime_short_sequence=[
                datetime(2001, 2, 3, 4, 5, 6),
                datetime(2001, 12, 13, 14, 15, 16),
            ],
        ),
    ]


@activity.defn
async def list_of_pydantic_models_activity(
    models: List[Union[MyPydanticModel, MyPydanticDatetimeModel]],
) -> List[Union[MyPydanticModel, MyPydanticDatetimeModel]]:
    return models


@workflow.defn
class ListOfPydanticObjectsWorkflow:
    @workflow.run
    async def run(
        self, models: List[Union[MyPydanticModel, MyPydanticDatetimeModel]]
    ) -> List[Union[MyPydanticModel, MyPydanticDatetimeModel]]:
        return await workflow.execute_activity(
            list_of_pydantic_models_activity,
            models,
            start_to_close_timeout=timedelta(minutes=1),
        )


async def test_field_conversion(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_pydantic_objects = make_pydantic_objects()

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[ListOfPydanticObjectsWorkflow],
        activities=[list_of_pydantic_models_activity],
    ):
        round_tripped_pydantic_objects = await client.execute_workflow(
            ListOfPydanticObjectsWorkflow.run,
            orig_pydantic_objects,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert orig_pydantic_objects == round_tripped_pydantic_objects


@dataclasses.dataclass
class MyDataClass:
    int_field: int


def make_dataclass_objects() -> List[MyDataClass]:
    return [MyDataClass(int_field=7)]


@workflow.defn
class MixedCollectionTypesWorkflow:
    @workflow.run
    async def run(
        self, input: Tuple[List[MyDataClass], List[MyPydanticModel]]
    ) -> Tuple[List[MyDataClass], List[MyPydanticModel]]:
        data_classes, pydantic_objects = input
        pydantic_objects = await workflow.execute_activity(
            list_of_pydantic_models_activity,
            pydantic_objects,
            start_to_close_timeout=timedelta(minutes=1),
        )
        return data_classes, pydantic_objects


async def test_mixed_collection_types(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_dataclass_objects = make_dataclass_objects()
    orig_pydantic_objects = make_pydantic_objects()

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[MixedCollectionTypesWorkflow],
        activities=[list_of_pydantic_models_activity],
    ):
        (
            round_tripped_dataclass_objects,
            round_tripped_pydantic_objects,
        ) = await client.execute_workflow(
            MixedCollectionTypesWorkflow.run,
            (orig_dataclass_objects, orig_pydantic_objects),
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert orig_dataclass_objects == round_tripped_dataclass_objects
    assert orig_pydantic_objects == round_tripped_pydantic_objects


@workflow.defn
class PydanticModelUsageWorkflow:
    @workflow.run
    async def run(self) -> None:
        for o in make_pydantic_objects():
            o._make_assertions()


async def test_pydantic_model_usage_in_workflow(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[PydanticModelUsageWorkflow],
    ):
        await client.execute_workflow(
            PydanticModelUsageWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )


@workflow.defn
class DatetimeUsageWorkflow:
    @workflow.run
    async def run(self) -> None:
        dt = workflow.now()
        assert isinstance(dt, datetime)
        assert issubclass(dt.__class__, datetime)
        for o in make_pydantic_objects():
            o._make_assertions()


async def test_datetime_usage_in_workflow(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[DatetimeUsageWorkflow],
    ):
        await client.execute_workflow(
            DatetimeUsageWorkflow.run,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
