import dataclasses
import uuid
from datetime import date, datetime, timedelta
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


class PydanticModel(BaseModel):
    ip_field: IPv4Address
    string_field_assigned_field: str = Field()
    string_field_with_default: str = Field(default_factory=lambda: "my-string")
    annotated_list_of_str: Annotated[
        List[str], Field(), WithJsonSchema({"extra": "data"})
    ]
    str_short_sequence: ShortSequence[List[str]]
    union_field: Union[int, str]

    def _check_instance(self):
        assert isinstance(self.ip_field, IPv4Address)
        assert isinstance(self.string_field_assigned_field, str)
        assert isinstance(self.string_field_with_default, str)
        assert isinstance(self.annotated_list_of_str, list)
        assert isinstance(self.str_short_sequence, list)
        assert self.annotated_list_of_str == ["my-string-1", "my-string-2"]
        assert self.str_short_sequence == ["my-string-1", "my-string-2"]
        assert isinstance(self.union_field, str)
        assert self.union_field == "my-string"


class PydanticDatetimeModel(BaseModel):
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

    def _check_instance(self):
        _assert_datetime_validity(self.datetime_field)
        _assert_datetime_validity(self.datetime_field_assigned_field)
        _assert_datetime_validity(self.datetime_field_with_default)
        _assert_datetime_validity(self.annotated_datetime)
        assert isinstance(self.annotated_list_of_datetime, list)
        assert isinstance(self.datetime_short_sequence, list)
        assert self.annotated_datetime == datetime(2000, 1, 2, 3, 4, 5)
        assert self.annotated_list_of_datetime == [
            datetime(2000, 1, 2, 3, 4, 5),
            datetime(2001, 11, 12, 13, 14, 15),
        ]
        assert self.datetime_short_sequence == [
            datetime(2000, 1, 2, 3, 4, 5),
            datetime(2001, 11, 12, 13, 14, 15),
        ]


class PydanticDateModel(BaseModel):
    date_field: date
    date_field_assigned_field: date = Field()
    date_field_with_default: date = Field(default_factory=lambda: date(2000, 1, 2))
    annotated_date: Annotated[date, Field(), WithJsonSchema({"extra": "data"})]
    annotated_list_of_date: Annotated[
        List[date], Field(), WithJsonSchema({"extra": "data"})
    ]
    date_short_sequence: ShortSequence[List[date]]

    def _check_instance(self):
        _assert_date_validity(self.date_field)
        _assert_date_validity(self.date_field_assigned_field)
        _assert_date_validity(self.date_field_with_default)
        _assert_date_validity(self.annotated_date)
        assert isinstance(self.annotated_list_of_date, list)
        assert isinstance(self.date_short_sequence, list)
        assert self.annotated_date == date(2000, 1, 2)
        assert self.annotated_list_of_date == [
            date(2000, 1, 2),
            date(2001, 11, 12),
        ]
        assert self.date_short_sequence == [
            date(2000, 1, 2),
            date(2001, 11, 12),
        ]


class PydanticTimedeltaModel(BaseModel):
    timedelta_field: timedelta
    timedelta_field_assigned_field: timedelta = Field()
    timedelta_field_with_default: timedelta = Field(
        default_factory=lambda: timedelta(days=1)
    )
    annotated_timedelta: Annotated[
        timedelta, Field(), WithJsonSchema({"extra": "data"})
    ]
    annotated_list_of_timedelta: Annotated[
        List[timedelta], Field(), WithJsonSchema({"extra": "data"})
    ]
    timedelta_short_sequence: ShortSequence[List[timedelta]]

    def _check_instance(self):
        _assert_timedelta_validity(self.timedelta_field)
        _assert_timedelta_validity(self.timedelta_field_assigned_field)
        _assert_timedelta_validity(self.timedelta_field_with_default)
        _assert_timedelta_validity(self.annotated_timedelta)
        assert isinstance(self.annotated_list_of_timedelta, list)
        for td in self.annotated_list_of_timedelta:
            _assert_timedelta_validity(td)
        assert isinstance(self.timedelta_short_sequence, list)
        for td in self.timedelta_short_sequence:
            _assert_timedelta_validity(td)
        assert self.annotated_timedelta == timedelta(1, 2, 3, 4, 5, 6, 7)
        assert self.annotated_list_of_timedelta == [
            timedelta(1, 2, 3, 4, 5, 6, 7),
            timedelta(2, 3, 4, 5, 6, 7, 8),
        ]


PydanticModels = Union[
    PydanticModel,
    PydanticDatetimeModel,
    PydanticDateModel,
    PydanticTimedeltaModel,
]


def _assert_datetime_validity(dt: datetime):
    assert isinstance(dt, datetime)
    assert issubclass(dt.__class__, datetime)


def _assert_date_validity(d: date):
    assert isinstance(d, date)
    assert issubclass(d.__class__, date)


def _assert_timedelta_validity(td: timedelta):
    assert isinstance(td, timedelta)
    assert issubclass(td.__class__, timedelta)


def make_homogeneous_list_of_pydantic_objects() -> List[PydanticModel]:
    return [
        PydanticModel(
            ip_field=IPv4Address("127.0.0.1"),
            string_field_assigned_field="my-string",
            annotated_list_of_str=["my-string-1", "my-string-2"],
            str_short_sequence=["my-string-1", "my-string-2"],
            union_field="my-string",
        ),
    ]


def make_heterogenous_list_of_pydantic_objects() -> List[PydanticModels]:
    return [
        PydanticModel(
            ip_field=IPv4Address("127.0.0.1"),
            string_field_assigned_field="my-string",
            annotated_list_of_str=["my-string-1", "my-string-2"],
            str_short_sequence=["my-string-1", "my-string-2"],
            union_field="my-string",
        ),
        PydanticDatetimeModel(
            datetime_field=datetime(2000, 1, 2, 3, 4, 5),
            datetime_field_assigned_field=datetime(2000, 1, 2, 3, 4, 5),
            annotated_datetime=datetime(2000, 1, 2, 3, 4, 5),
            annotated_list_of_datetime=[
                datetime(2000, 1, 2, 3, 4, 5),
                datetime(2001, 11, 12, 13, 14, 15),
            ],
            datetime_short_sequence=[
                datetime(2000, 1, 2, 3, 4, 5),
                datetime(2001, 11, 12, 13, 14, 15),
            ],
        ),
        PydanticDateModel(
            date_field=date(2000, 1, 2),
            date_field_assigned_field=date(2000, 1, 2),
            annotated_date=date(2000, 1, 2),
            annotated_list_of_date=[date(2000, 1, 2), date(2001, 11, 12)],
            date_short_sequence=[date(2000, 1, 2), date(2001, 11, 12)],
        ),
        PydanticTimedeltaModel(
            timedelta_field=timedelta(1, 2, 3, 4, 5, 6, 7),
            timedelta_field_assigned_field=timedelta(1, 2, 3, 4, 5, 6, 7),
            annotated_timedelta=timedelta(1, 2, 3, 4, 5, 6, 7),
            annotated_list_of_timedelta=[
                timedelta(1, 2, 3, 4, 5, 6, 7),
                timedelta(2, 3, 4, 5, 6, 7, 8),
            ],
            timedelta_short_sequence=[
                timedelta(1, 2, 3, 4, 5, 6, 7),
                timedelta(2, 3, 4, 5, 6, 7, 8),
            ],
        ),
    ]


@activity.defn
async def homogeneous_list_of_pydantic_models_activity(
    models: List[PydanticModel],
) -> List[PydanticModel]:
    return models


@activity.defn
async def heterogeneous_list_of_pydantic_models_activity(
    models: List[PydanticModels],
) -> List[PydanticModels]:
    return models


@workflow.defn
class HomogenousListOfPydanticObjectsWorkflow:
    @workflow.run
    async def run(self, models: List[PydanticModel]) -> List[PydanticModel]:
        return await workflow.execute_activity(
            homogeneous_list_of_pydantic_models_activity,
            models,
            start_to_close_timeout=timedelta(minutes=1),
        )


@workflow.defn
class HeterogenousListOfPydanticObjectsWorkflow:
    @workflow.run
    async def run(self, models: List[PydanticModels]) -> List[PydanticModels]:
        return await workflow.execute_activity(
            heterogeneous_list_of_pydantic_models_activity,
            models,
            start_to_close_timeout=timedelta(minutes=1),
        )


async def test_homogeneous_list_of_pydantic_objects(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_pydantic_objects = make_homogeneous_list_of_pydantic_objects()

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[HomogenousListOfPydanticObjectsWorkflow],
        activities=[homogeneous_list_of_pydantic_models_activity],
    ):
        round_tripped_pydantic_objects = await client.execute_workflow(
            HomogenousListOfPydanticObjectsWorkflow.run,
            orig_pydantic_objects,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert orig_pydantic_objects == round_tripped_pydantic_objects


async def test_heterogenous_list_of_pydantic_objects(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_pydantic_objects = make_heterogenous_list_of_pydantic_objects()

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[HeterogenousListOfPydanticObjectsWorkflow],
        activities=[heterogeneous_list_of_pydantic_models_activity],
    ):
        round_tripped_pydantic_objects = await client.execute_workflow(
            HeterogenousListOfPydanticObjectsWorkflow.run,
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
        self,
        input: Tuple[
            List[MyDataClass],
            List[PydanticModels],
        ],
    ) -> Tuple[
        List[MyDataClass],
        List[PydanticModels],
    ]:
        data_classes, pydantic_objects = input
        pydantic_objects = await workflow.execute_activity(
            heterogeneous_list_of_pydantic_models_activity,
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
    orig_pydantic_objects = make_heterogenous_list_of_pydantic_objects()

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[MixedCollectionTypesWorkflow],
        activities=[heterogeneous_list_of_pydantic_models_activity],
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
        for o in make_heterogenous_list_of_pydantic_objects():
            o._check_instance()


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
