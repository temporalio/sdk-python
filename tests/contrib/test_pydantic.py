import collections
import dataclasses
import decimal
import fractions
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from enum import Enum, IntEnum
from ipaddress import IPv4Address
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Dict,
    Generic,
    Hashable,
    List,
    Optional,
    Pattern,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from annotated_types import Len
from pydantic import BaseModel, Field, WithJsonSchema

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

SequenceType = TypeVar("SequenceType", bound=Sequence[Any])
ShortSequence = Annotated[SequenceType, Len(max_length=2)]


class FruitEnum(str, Enum):
    apple = "apple"
    banana = "banana"


class NumberEnum(IntEnum):
    one = 1
    two = 2


class StandardTypesModel(BaseModel):
    # Boolean
    bool_field: bool
    bool_field_int: bool
    bool_field_str: bool

    # Numbers
    int_field: int
    float_field: float
    decimal_field: decimal.Decimal
    complex_field: complex
    fraction_field: fractions.Fraction

    # Strings and Bytes
    str_field: str
    bytes_field: bytes

    # None
    none_field: None

    # Enums
    str_enum_field: FruitEnum
    int_enum_field: NumberEnum

    # Collections
    list_field: list
    tuple_field: tuple
    set_field: set
    frozenset_field: frozenset
    deque_field: collections.deque
    # array_field: array.array

    # Mappings
    dict_field: dict
    # defaultdict_field: collections.defaultdict
    counter_field: collections.Counter

    # Other Types
    pattern_field: Pattern
    hashable_field: Hashable
    any_field: Any
    # callable_field: Callable

    def _check_instance(self) -> None:
        # Boolean checks
        assert isinstance(self.bool_field, bool)
        assert self.bool_field is True
        assert isinstance(self.bool_field_int, bool)
        assert self.bool_field_int is True
        assert isinstance(self.bool_field_str, bool)
        assert self.bool_field_str is True

        # Number checks
        assert isinstance(self.int_field, int)
        assert self.int_field == 42
        assert isinstance(self.float_field, float)
        assert self.float_field == 3.14
        assert isinstance(self.decimal_field, decimal.Decimal)
        assert self.decimal_field == decimal.Decimal("3.14")
        assert isinstance(self.complex_field, complex)
        assert self.complex_field == complex(1, 2)
        assert isinstance(self.fraction_field, fractions.Fraction)
        assert self.fraction_field == fractions.Fraction(22, 7)

        # String and Bytes checks
        assert isinstance(self.str_field, str)
        assert self.str_field == "hello"
        assert isinstance(self.bytes_field, bytes)
        assert self.bytes_field == b"world"

        # None check
        assert self.none_field is None

        # Enum checks
        assert isinstance(self.str_enum_field, Enum)
        assert isinstance(self.int_enum_field, IntEnum)

        # Collection checks
        assert isinstance(self.list_field, list)
        assert self.list_field == [1, 2, 3]
        assert isinstance(self.tuple_field, tuple)
        assert self.tuple_field == (1, 2, 3)
        assert isinstance(self.set_field, set)
        assert self.set_field == {1, 2, 3}
        assert isinstance(self.frozenset_field, frozenset)
        assert self.frozenset_field == frozenset([1, 2, 3])
        assert isinstance(self.deque_field, collections.deque)
        assert list(self.deque_field) == [1, 2, 3]
        # assert isinstance(self.array_field, array.array)
        # assert list(self.array_field) == [1, 2, 3]

        # Mapping checks
        assert isinstance(self.dict_field, dict)
        assert self.dict_field == {"a": 1, "b": 2}
        # assert isinstance(self.defaultdict_field, collections.defaultdict)
        # assert dict(self.defaultdict_field) == {"a": 1, "b": 2}
        assert isinstance(self.counter_field, collections.Counter)
        assert dict(self.counter_field) == {"a": 1, "b": 2}

        # Other type checks
        assert isinstance(self.pattern_field, Pattern)
        assert self.pattern_field.pattern == r"\d+"
        assert isinstance(self.hashable_field, Hashable)
        assert self.hashable_field == "test"
        assert self.any_field == "anything goes"
        # assert callable(self.callable_field)


def make_standard_types_object() -> StandardTypesModel:
    return StandardTypesModel(
        # Boolean
        bool_field=True,
        bool_field_int=1,  # type: ignore
        bool_field_str="true",  # type: ignore
        # Numbers
        int_field=42,
        float_field=3.14,
        decimal_field=decimal.Decimal("3.14"),
        complex_field=complex(1, 2),
        fraction_field=fractions.Fraction(22, 7),
        # Strings and Bytes
        str_field="hello",
        bytes_field=b"world",
        # None
        none_field=None,
        # Enums
        str_enum_field=FruitEnum.apple,
        int_enum_field=NumberEnum.one,
        # Collections
        list_field=[1, 2, 3],
        tuple_field=(1, 2, 3),
        set_field={1, 2, 3},
        frozenset_field=frozenset([1, 2, 3]),
        deque_field=collections.deque([1, 2, 3]),
        # array_field=array.array("i", [1, 2, 3]),
        # Mappings
        dict_field={"a": 1, "b": 2},
        # defaultdict_field=collections.defaultdict(int, {"a": 1, "b": 2}),
        counter_field=collections.Counter({"a": 1, "b": 2}),
        # Other Types
        pattern_field=re.compile(r"\d+"),
        hashable_field="test",
        any_field="anything goes",
        # callable_field=lambda x: x,
    )


class ComplexTypesModel(BaseModel):
    list_field: List[str]
    dict_field: Dict[str, int]
    set_field: Set[int]
    tuple_field: Tuple[str, int]
    union_field: Union[str, int]
    optional_field: Optional[str]

    def _check_instance(self) -> None:
        assert isinstance(self.list_field, list)
        assert isinstance(self.dict_field, dict)
        assert isinstance(self.set_field, set)
        assert isinstance(self.tuple_field, tuple)
        assert isinstance(self.union_field, str)
        assert isinstance(self.optional_field, str)
        assert self.list_field == ["a", "b", "c"]
        assert self.dict_field == {"x": 1, "y": 2}
        assert self.set_field == {1, 2, 3}
        assert self.tuple_field == ("hello", 42)
        assert self.union_field == "string_or_int"
        assert self.optional_field == "present"


def make_complex_types_object() -> ComplexTypesModel:
    return ComplexTypesModel(
        list_field=["a", "b", "c"],
        dict_field={"x": 1, "y": 2},
        set_field={1, 2, 3},
        tuple_field=("hello", 42),
        union_field="string_or_int",
        optional_field="present",
    )


class SpecialTypesModel(BaseModel):
    datetime_field: datetime
    datetime_field_int: datetime
    datetime_field_float: datetime
    datetime_field_str_formatted: datetime
    datetime_field_str_int: datetime
    datetime_field_date: datetime
    date_field: date
    timedelta_field: timedelta
    path_field: Path
    uuid_field: uuid.UUID
    ip_field: IPv4Address

    def _check_instance(self) -> None:
        dt = datetime(2000, 1, 2, 3, 4, 5)
        dtz = datetime(2000, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        assert isinstance(self.datetime_field, datetime)
        assert isinstance(self.datetime_field_int, datetime)
        assert isinstance(self.datetime_field_float, datetime)
        assert isinstance(self.datetime_field_str_formatted, datetime)
        assert isinstance(self.datetime_field_str_int, datetime)
        assert isinstance(self.datetime_field_date, datetime)
        assert isinstance(self.timedelta_field, timedelta)
        assert isinstance(self.path_field, Path)
        assert isinstance(self.uuid_field, uuid.UUID)
        assert isinstance(self.ip_field, IPv4Address)
        assert self.datetime_field == dt
        assert self.datetime_field_int == dtz
        assert self.datetime_field_float == dtz
        assert self.datetime_field_str_formatted == dtz
        assert self.datetime_field_str_int == dtz
        assert self.datetime_field_date == datetime(2000, 1, 2)
        assert self.date_field == date(2000, 1, 2)
        assert self.timedelta_field == timedelta(days=1, hours=2)
        assert self.path_field == Path("test/path")
        assert self.uuid_field == uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert self.ip_field == IPv4Address("127.0.0.1")


def make_special_types_object() -> SpecialTypesModel:
    return SpecialTypesModel(
        datetime_field=datetime(2000, 1, 2, 3, 4, 5),
        # 946800245
        datetime_field_int=946782245,  # type: ignore
        datetime_field_float=946782245.0,  # type: ignore
        datetime_field_str_formatted="2000-01-02T03:04:05Z",  # type: ignore
        datetime_field_str_int="946782245",  # type: ignore
        datetime_field_date=datetime(2000, 1, 2),
        date_field=date(2000, 1, 2),
        timedelta_field=timedelta(days=1, hours=2),
        path_field=Path("test/path"),
        uuid_field=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        ip_field=IPv4Address("127.0.0.1"),
    )


class ChildModel(BaseModel):
    name: str
    value: int


class ParentModel(BaseModel):
    child: ChildModel
    children: List[ChildModel]

    def _check_instance(self) -> None:
        assert isinstance(self.child, ChildModel)
        assert isinstance(self.children, list)
        assert all(isinstance(child, ChildModel) for child in self.children)
        assert self.child.name == "child1"
        assert self.child.value == 1
        assert len(self.children) == 2
        assert self.children[0].name == "child2"
        assert self.children[0].value == 2
        assert self.children[1].name == "child3"
        assert self.children[1].value == 3


def make_nested_object() -> ParentModel:
    return ParentModel(
        child=ChildModel(name="child1", value=1),
        children=[
            ChildModel(name="child2", value=2),
            ChildModel(name="child3", value=3),
        ],
    )


class FieldFeaturesModel(BaseModel):
    field_with_default: str = "default"
    field_with_factory: datetime = Field(
        default_factory=lambda: datetime(2000, 1, 2, 3, 4, 5)
    )
    field_with_constraints: int = Field(gt=0, lt=100)
    field_with_alias: str = Field(alias="different_name")

    def _check_instance(self) -> None:
        assert isinstance(self.field_with_default, str)
        assert isinstance(self.field_with_factory, datetime)
        assert isinstance(self.field_with_constraints, int)
        assert isinstance(self.field_with_alias, str)
        assert self.field_with_default == "default"
        assert 0 < self.field_with_constraints < 100
        assert self.field_with_alias == "aliased_value"


def make_field_features_object() -> FieldFeaturesModel:
    return FieldFeaturesModel(
        field_with_constraints=50,
        different_name="aliased_value",
    )


class AnnotatedFieldsModel(BaseModel):
    max_length_str: Annotated[str, Len(max_length=10)]
    custom_json: Annotated[Dict[str, Any], WithJsonSchema({"extra": "data"})]

    def _check_instance(self) -> None:
        assert isinstance(self.max_length_str, str)
        assert isinstance(self.custom_json, dict)
        assert len(self.max_length_str) <= 10
        assert self.max_length_str == "short"
        assert self.custom_json == {"key": "value"}


def make_annotated_fields_object() -> AnnotatedFieldsModel:
    return AnnotatedFieldsModel(
        max_length_str="short",
        custom_json={"key": "value"},
    )


T = TypeVar("T")


class GenericModel(BaseModel, Generic[T]):
    value: T
    values: List[T]

    def _check_instance(self) -> None:
        assert isinstance(self.value, str)
        assert isinstance(self.values, list)
        assert all(isinstance(v, str) for v in self.values)
        assert self.value == "single"
        assert self.values == ["multiple", "values"]


def make_generic_string_object() -> GenericModel[str]:
    return GenericModel[str](
        value="single",
        values=["multiple", "values"],
    )


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


def make_pydantic_datetime_object() -> PydanticDatetimeModel:
    return PydanticDatetimeModel(
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
    )


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


def make_pydantic_date_object() -> PydanticDateModel:
    return PydanticDateModel(
        date_field=date(2000, 1, 2),
        date_field_assigned_field=date(2000, 1, 2),
        annotated_date=date(2000, 1, 2),
        annotated_list_of_date=[date(2000, 1, 2), date(2001, 11, 12)],
        date_short_sequence=[date(2000, 1, 2), date(2001, 11, 12)],
    )


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


def make_pydantic_timedelta_object() -> PydanticTimedeltaModel:
    return PydanticTimedeltaModel(
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
    )


HeterogeneousPydanticModels = Union[
    StandardTypesModel,
    ComplexTypesModel,
    SpecialTypesModel,
    ParentModel,
    FieldFeaturesModel,
    AnnotatedFieldsModel,
    GenericModel[Any],
    PydanticDatetimeModel,
    PydanticDateModel,
    PydanticTimedeltaModel,
]


HomogeneousPydanticModels = SpecialTypesModel


def _assert_datetime_validity(dt: datetime):
    assert isinstance(dt, datetime)
    assert issubclass(dt.__class__, datetime)


def _assert_date_validity(d: date):
    assert isinstance(d, date)
    assert issubclass(d.__class__, date)


def _assert_timedelta_validity(td: timedelta):
    assert isinstance(td, timedelta)
    assert issubclass(td.__class__, timedelta)


def make_homogeneous_list_of_pydantic_objects() -> List[HomogeneousPydanticModels]:
    objects = [make_special_types_object()]
    for o in objects:
        o._check_instance()
    return objects


def make_heterogeneous_list_of_pydantic_objects() -> List[HeterogeneousPydanticModels]:
    objects = [
        make_standard_types_object(),
        make_complex_types_object(),
        make_special_types_object(),
        make_nested_object(),
        make_field_features_object(),
        make_annotated_fields_object(),
        make_generic_string_object(),
        make_pydantic_datetime_object(),
        make_pydantic_date_object(),
        make_pydantic_timedelta_object(),
    ]
    for o in objects:
        o._check_instance()  # type: ignore
    return objects  # type: ignore


@activity.defn
async def homogeneous_list_of_pydantic_models_activity(
    models: List[HomogeneousPydanticModels],
) -> List[HomogeneousPydanticModels]:
    return models


@activity.defn
async def heterogeneous_list_of_pydantic_models_activity(
    models: List[HeterogeneousPydanticModels],
) -> List[HeterogeneousPydanticModels]:
    return models


@workflow.defn
class HomogeneousListOfPydanticObjectsWorkflow:
    @workflow.run
    async def run(
        self, models: List[HomogeneousPydanticModels]
    ) -> List[HomogeneousPydanticModels]:
        return await workflow.execute_activity(
            homogeneous_list_of_pydantic_models_activity,
            models,
            start_to_close_timeout=timedelta(minutes=1),
        )


@workflow.defn
class HeterogeneousListOfPydanticObjectsWorkflow:
    @workflow.run
    async def run(
        self, models: List[HeterogeneousPydanticModels]
    ) -> List[HeterogeneousPydanticModels]:
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
        workflows=[HomogeneousListOfPydanticObjectsWorkflow],
        activities=[homogeneous_list_of_pydantic_models_activity],
    ):
        round_tripped_pydantic_objects = await client.execute_workflow(
            HomogeneousListOfPydanticObjectsWorkflow.run,
            orig_pydantic_objects,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert orig_pydantic_objects == round_tripped_pydantic_objects
    for o in round_tripped_pydantic_objects:
        o._check_instance()


async def test_heterogeneous_list_of_pydantic_objects(client: Client):
    new_config = client.config()
    new_config["data_converter"] = pydantic_data_converter
    client = Client(**new_config)
    task_queue_name = str(uuid.uuid4())

    orig_pydantic_objects = make_heterogeneous_list_of_pydantic_objects()

    async with Worker(
        client,
        task_queue=task_queue_name,
        workflows=[HeterogeneousListOfPydanticObjectsWorkflow],
        activities=[heterogeneous_list_of_pydantic_models_activity],
    ):
        round_tripped_pydantic_objects = await client.execute_workflow(
            HeterogeneousListOfPydanticObjectsWorkflow.run,
            orig_pydantic_objects,
            id=str(uuid.uuid4()),
            task_queue=task_queue_name,
        )
    assert orig_pydantic_objects == round_tripped_pydantic_objects
    for o in round_tripped_pydantic_objects:
        o._check_instance()


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
            List[HeterogeneousPydanticModels],
        ],
    ) -> Tuple[
        List[MyDataClass],
        List[HeterogeneousPydanticModels],
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
    orig_pydantic_objects = make_heterogeneous_list_of_pydantic_objects()

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
    for o in round_tripped_pydantic_objects:
        o._check_instance()


@workflow.defn
class PydanticModelUsageWorkflow:
    @workflow.run
    async def run(self) -> None:
        for o in make_heterogeneous_list_of_pydantic_objects():
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
