import collections
import decimal
import fractions
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum, IntEnum
from ipaddress import IPv4Address
from pathlib import Path
from typing import (
    Any,
    Dict,
    Hashable,
    List,
    NamedTuple,
    Optional,
    Pattern,
    Sequence,
    Set,
    Tuple,
    Union,
)

from pydantic import BaseModel
from typing_extensions import TypedDict


class FruitEnum(str, Enum):
    apple = "apple"
    banana = "banana"


class NumberEnum(IntEnum):
    one = 1
    two = 2


class UserTypedDict(TypedDict):
    name: str
    id: int


class TypedDictModel(BaseModel):
    typed_dict_field: UserTypedDict

    def _check_instance(self) -> None:
        assert isinstance(self.typed_dict_field, dict)
        assert self.typed_dict_field == {"name": "username", "id": 7}


def make_typed_dict_object() -> TypedDictModel:
    return TypedDictModel(typed_dict_field={"name": "username", "id": 7})


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
    sequence_field: Sequence[int]
    # Iterable[int] supported but not tested since original vs round-tripped do not compare equal

    # Mappings
    dict_field: dict
    # defaultdict_field: collections.defaultdict
    counter_field: collections.Counter
    typed_dict_field: UserTypedDict

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
        assert isinstance(self.sequence_field, list)
        assert list(self.sequence_field) == [1, 2, 3]

        # Mapping checks
        assert isinstance(self.dict_field, dict)
        assert self.dict_field == {"a": 1, "b": 2}
        # assert isinstance(self.defaultdict_field, collections.defaultdict)
        # assert dict(self.defaultdict_field) == {"a": 1, "b": 2}
        assert isinstance(self.counter_field, collections.Counter)
        assert dict(self.counter_field) == {"a": 1, "b": 2}
        assert isinstance(self.typed_dict_field, dict)
        assert self.typed_dict_field == {"name": "username", "id": 7}

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
        # these cast input to list, tuple, set, etc.
        list_field={1, 2, 3},  # type: ignore
        tuple_field=(1, 2, 3),
        set_field={1, 2, 3},
        frozenset_field=frozenset([1, 2, 3]),
        deque_field=collections.deque([1, 2, 3]),
        # other sequence types are converted to list, as documented
        sequence_field=[1, 2, 3],
        # Mappings
        dict_field={"a": 1, "b": 2},
        # defaultdict_field=collections.defaultdict(int, {"a": 1, "b": 2}),
        counter_field=collections.Counter({"a": 1, "b": 2}),
        typed_dict_field={"name": "username", "id": 7},
        # Other Types
        pattern_field=re.compile(r"\d+"),
        hashable_field="test",
        any_field="anything goes",
        # callable_field=lambda x: x,
    )


class Point(NamedTuple):
    x: int
    y: int


class ComplexTypesModel(BaseModel):
    list_field: List[str]
    dict_field: Dict[str, int]
    set_field: Set[int]
    tuple_field: Tuple[str, int]
    union_field: Union[str, int]
    optional_field: Optional[str]
    named_tuple_field: Point

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
        assert self.named_tuple_field == Point(x=1, y=2)


def make_complex_types_object() -> ComplexTypesModel:
    return ComplexTypesModel(
        list_field=["a", "b", "c"],
        dict_field={"x": 1, "y": 2},
        set_field={1, 2, 3},
        tuple_field=("hello", 42),
        union_field="string_or_int",
        optional_field="present",
        named_tuple_field=Point(x=1, y=2),
    )


class SpecialTypesModel(BaseModel):
    datetime_field: datetime
    datetime_field_int: datetime
    datetime_field_float: datetime
    datetime_field_str_formatted: datetime
    datetime_field_str_int: datetime
    datetime_field_date: datetime

    time_field: time
    time_field_str: time

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
        assert self.time_field == time(3, 4, 5)
        assert self.time_field_str == time(3, 4, 5, tzinfo=timezone.utc)
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
        time_field=time(3, 4, 5),
        time_field_str="03:04:05Z",  # type: ignore
        date_field=date(2000, 1, 2),
        timedelta_field=timedelta(days=1, hours=2),
        path_field=Path("test/path"),
        uuid_field=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        ip_field=IPv4Address("127.0.0.1"),
    )
