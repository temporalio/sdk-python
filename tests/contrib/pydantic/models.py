import dataclasses
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Dict,
    Generic,
    List,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

from annotated_types import Len
from pydantic import BaseModel, Field, WithJsonSchema

from temporalio import workflow

# Define some of the models outside the sandbox
with workflow.unsafe.imports_passed_through():
    from tests.contrib.pydantic.models_2 import (
        ComplexTypesModel,
        SpecialTypesModel,
        StandardTypesModel,
        make_complex_types_object,
        make_special_types_object,
        make_standard_types_object,
    )

SequenceType = TypeVar("SequenceType", bound=Sequence[Any])
ShortSequence = Annotated[SequenceType, Len(max_length=2)]


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


class UnionModel(BaseModel):
    simple_union_field: Union[str, int]
    proxied_union_field: Union[datetime, Path]

    def _check_instance(self) -> None:
        assert isinstance(self.simple_union_field, str)
        assert self.simple_union_field == "string_or_int"
        assert isinstance(self.proxied_union_field, Path)
        assert self.proxied_union_field == Path("test/path")


def make_union_object() -> UnionModel:
    return UnionModel(
        simple_union_field="string_or_int",
        proxied_union_field=Path("test/path"),
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


def _assert_datetime_validity(dt: datetime):
    assert isinstance(dt, datetime)
    assert issubclass(dt.__class__, datetime)


def _assert_date_validity(d: date):
    assert isinstance(d, date)
    assert issubclass(d.__class__, date)


def _assert_timedelta_validity(td: timedelta):
    assert isinstance(td, timedelta)
    assert issubclass(td.__class__, timedelta)


PydanticModels = Union[
    StandardTypesModel,
    ComplexTypesModel,
    SpecialTypesModel,
    ParentModel,
    FieldFeaturesModel,
    AnnotatedFieldsModel,
    GenericModel[Any],
    UnionModel,
    PydanticDatetimeModel,
    PydanticDateModel,
    PydanticTimedeltaModel,
]


def make_list_of_pydantic_objects() -> List[PydanticModels]:
    objects = [
        make_standard_types_object(),
        make_complex_types_object(),
        make_special_types_object(),
        make_nested_object(),
        make_field_features_object(),
        make_annotated_fields_object(),
        make_generic_string_object(),
        make_union_object(),
        make_pydantic_datetime_object(),
        make_pydantic_date_object(),
        make_pydantic_timedelta_object(),
    ]
    for o in objects:
        o._check_instance()  # type: ignore
    return objects  # type: ignore


@dataclasses.dataclass(order=True)
class MyDataClass:
    # The name int_field also occurs in StandardTypesModel and currently unions can match them up incorrectly.
    data_class_int_field: int


def make_dataclass_objects() -> List[MyDataClass]:
    return [MyDataClass(data_class_int_field=7)]


ComplexCustomType = Tuple[List[MyDataClass], List[PydanticModels]]
ComplexCustomUnionType = List[Union[MyDataClass, PydanticModels]]
