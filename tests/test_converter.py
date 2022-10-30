from __future__ import annotations

import dataclasses
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import (
    Any,
    Deque,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    NewType,
    Optional,
    Sequence,
    Set,
    Text,
    Tuple,
    Type,
    Union,
)

import pydantic
import pytest
from typing_extensions import Literal, TypedDict

import temporalio.api.common.v1
import temporalio.common
import temporalio.converter
from temporalio.api.common.v1 import Payload as AnotherNameForPayload
from temporalio.api.common.v1.message_pb2 import Payload


class NonSerializableClass:
    pass


class NonSerializableEnum(Enum):
    FOO = "foo"


class SerializableEnum(IntEnum):
    FOO = 1


@dataclass
class MyDataClass:
    foo: str
    bar: int
    baz: SerializableEnum


async def test_converter_default():
    async def assert_payload(
        input,
        expected_encoding,
        expected_data,
        *,
        expected_decoded_input=None,
        type_hint=None,
    ):
        payloads = await temporalio.converter.DataConverter().encode([input])
        # Check encoding and data
        assert len(payloads) == 1
        if isinstance(expected_encoding, str):
            expected_encoding = expected_encoding.encode()
        assert payloads[0].metadata["encoding"] == expected_encoding
        if isinstance(expected_data, str):
            expected_data = expected_data.encode()
        assert payloads[0].data == expected_data
        # Decode and check
        actual_inputs = await temporalio.converter.DataConverter().decode(
            payloads, [type_hint]
        )
        assert len(actual_inputs) == 1
        if expected_decoded_input is None:
            expected_decoded_input = input
        assert type(actual_inputs[0]) is type(expected_decoded_input)
        assert actual_inputs[0] == expected_decoded_input
        return payloads[0]

    # Basic types
    await assert_payload(None, "binary/null", "")
    await assert_payload(b"some binary", "binary/plain", "some binary")
    payload = await assert_payload(
        temporalio.api.common.v1.WorkflowExecution(workflow_id="id1", run_id="id2"),
        "json/protobuf",
        '{"runId":"id2","workflowId":"id1"}',
    )
    assert (
        payload.metadata["messageType"] == b"temporal.api.common.v1.WorkflowExecution"
    )
    await assert_payload(
        {"foo": "bar", "baz": "qux"}, "json/plain", '{"baz":"qux","foo":"bar"}'
    )
    await assert_payload("somestr", "json/plain", '"somestr"')
    await assert_payload(1234, "json/plain", "1234")
    await assert_payload(12.34, "json/plain", "12.34")
    await assert_payload(True, "json/plain", "true")
    await assert_payload(False, "json/plain", "false")

    # Unknown type
    with pytest.raises(TypeError) as excinfo:
        await assert_payload(NonSerializableClass(), None, None)
    assert "not JSON serializable" in str(excinfo.value)

    # Bad enum type. We do not allow non-int enums due to ambiguity in
    # rebuilding and other confusion.
    with pytest.raises(TypeError) as excinfo:
        await assert_payload(NonSerializableEnum.FOO, None, None)
    assert "not JSON serializable" in str(excinfo.value)

    # Good enum no type hint
    await assert_payload(
        SerializableEnum.FOO, "json/plain", "1", expected_decoded_input=1
    )

    # Good enum type hint
    await assert_payload(
        SerializableEnum.FOO, "json/plain", "1", type_hint=SerializableEnum
    )

    # Data class without type hint is just dict
    await assert_payload(
        MyDataClass(foo="somestr", bar=123, baz=SerializableEnum.FOO),
        "json/plain",
        '{"bar":123,"baz":1,"foo":"somestr"}',
        expected_decoded_input={"foo": "somestr", "bar": 123, "baz": 1},
    )

    # Data class with type hint reconstructs the class
    await assert_payload(
        MyDataClass(foo="somestr", bar=123, baz=SerializableEnum.FOO),
        "json/plain",
        '{"bar":123,"baz":1,"foo":"somestr"}',
        type_hint=MyDataClass,
    )


def test_binary_proto():
    # We have to test this separately because by default it never encodes
    # anything since JSON proto takes precedence
    conv = temporalio.converter.BinaryProtoPayloadConverter()
    proto = temporalio.api.common.v1.WorkflowExecution(workflow_id="id1", run_id="id2")
    payload = conv.to_payload(proto)
    assert payload.metadata["encoding"] == b"binary/protobuf"
    assert (
        payload.metadata["messageType"] == b"temporal.api.common.v1.WorkflowExecution"
    )
    assert payload.data == proto.SerializeToString()
    decoded = conv.from_payload(payload)
    assert decoded == proto


def test_encode_search_attribute_values():
    with pytest.raises(TypeError, match="of type tuple not one of"):
        temporalio.converter.encode_search_attribute_values([("bad type",)])
    with pytest.raises(ValueError, match="Timezone must be present"):
        temporalio.converter.encode_search_attribute_values([datetime.utcnow()])
    with pytest.raises(TypeError, match="must have the same type"):
        temporalio.converter.encode_search_attribute_values(["foo", 123])


def test_decode_search_attributes():
    """Tests decode from protobuf for python types"""

    def payload(key, dtype, data, encoding=None):
        if encoding is None:
            encoding = {"encoding": b"json/plain"}
        check = Payload(
            data=bytes(data, encoding="utf-8"),
            metadata={"type": bytes(dtype, encoding="utf-8"), **encoding},
        )
        return temporalio.api.common.v1.SearchAttributes(indexed_fields={key: check})

    # Check basic keyword parsing works
    kw_check = temporalio.converter.decode_search_attributes(
        payload("kw", "Keyword", '"test-id"')
    )
    assert kw_check["kw"][0] == "test-id"

    # Ensure original DT functionality works
    dt_check = temporalio.converter.decode_search_attributes(
        payload("dt", "Datetime", '"2020-01-01T00:00:00"')
    )
    assert dt_check["dt"][0] == datetime(2020, 1, 1, 0, 0, 0)

    # Check timezone aware works as server is using ISO 8601
    dttz_check = temporalio.converter.decode_search_attributes(
        payload("dt", "Datetime", '"2020-01-01T00:00:00Z"')
    )
    assert dttz_check["dt"][0] == datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


NewIntType = NewType("NewIntType", int)
MyDataClassAlias = MyDataClass


@dataclass
class NestedDataClass:
    foo: str
    bar: List[NestedDataClass] = dataclasses.field(default_factory=list)
    baz: Optional[NestedDataClass] = None


class MyTypedDict(TypedDict):
    foo: str
    bar: MyDataClass


class MyTypedDictNotTotal(TypedDict, total=False):
    foo: str
    bar: MyDataClass


class MyPydanticClass(pydantic.BaseModel):
    foo: str
    bar: List[MyPydanticClass]


def test_json_type_hints():
    converter = temporalio.converter.JSONPlainPayloadConverter()

    def ok(
        hint: Type, value: Any, expected_result: Any = temporalio.common._arg_unset
    ) -> None:
        payload = converter.to_payload(value)
        converted_value = converter.from_payload(payload, hint)
        if expected_result is not temporalio.common._arg_unset:
            assert expected_result == converted_value
        else:
            assert converted_value == value

    def fail(hint: Type, value: Any) -> None:
        with pytest.raises(Exception):
            payload = converter.to_payload(value)
            converter.from_payload(payload, hint)

    # Primitives
    ok(int, 5)
    ok(int, 5.5, 5)
    ok(float, 5, 5.0)
    ok(float, 5.5)
    ok(bool, True)
    ok(str, "foo")
    ok(Text, "foo")
    ok(bytes, b"foo")
    fail(int, "1")
    fail(float, "1")
    fail(bool, "1")
    fail(str, 1)

    # Any
    ok(Any, 5)
    ok(Any, None)

    # Literal
    ok(Literal["foo"], "foo")
    ok(Literal["foo", False], False)
    fail(Literal["foo", "bar"], "baz")

    # Dataclass
    ok(MyDataClass, MyDataClass("foo", 5, SerializableEnum.FOO))
    ok(NestedDataClass, NestedDataClass("foo"))
    ok(NestedDataClass, NestedDataClass("foo", baz=NestedDataClass("bar")))
    ok(NestedDataClass, NestedDataClass("foo", bar=[NestedDataClass("bar")]))
    # Missing required dataclass fields causes failure
    ok(NestedDataClass, {"foo": "bar"}, NestedDataClass("bar"))
    fail(NestedDataClass, {})
    # Additional dataclass fields is ok
    ok(NestedDataClass, {"foo": "bar", "unknownfield": "baz"}, NestedDataClass("bar"))

    # Optional/Union
    ok(Optional[int], 5)
    ok(Optional[int], None)
    ok(Optional[MyDataClass], MyDataClass("foo", 5, SerializableEnum.FOO))
    ok(Union[int, str], 5)
    ok(Union[int, str], "foo")
    ok(Union[MyDataClass, NestedDataClass], MyDataClass("foo", 5, SerializableEnum.FOO))
    ok(Union[MyDataClass, NestedDataClass], NestedDataClass("foo"))

    # NewType
    ok(NewIntType, 5)

    # List-like
    ok(List, [5])
    ok(List[int], [5])
    ok(List[MyDataClass], [MyDataClass("foo", 5, SerializableEnum.FOO)])
    ok(Iterable[int], [5, 6])
    ok(Tuple[int, str], (5, "6"))
    ok(Tuple[int, ...], (5, 6, 7))
    ok(Set[int], set([5, 6]))
    ok(Set, set([5, 6]))
    ok(List, ["foo"])
    ok(Deque[int], deque([5, 6]))
    ok(Sequence[int], [5, 6])
    fail(List[int], [1, 2, "3"])

    # Dict-like
    ok(Dict[str, MyDataClass], {"foo": MyDataClass("foo", 5, SerializableEnum.FOO)})
    ok(Dict, {"foo": 123})
    ok(Dict[str, Any], {"foo": 123})
    ok(Dict[Any, int], {"foo": 123})
    ok(Mapping, {"foo": 123})
    ok(Mapping[str, int], {"foo": 123})
    ok(MutableMapping[str, int], {"foo": 123})
    ok(
        MyTypedDict,
        MyTypedDict(foo="somestr", bar=MyDataClass("foo", 5, SerializableEnum.FOO)),
    )
    # TypedDict allows all sorts of dicts, even if they are missing required
    # fields or have unknown fields. This matches Python runtime behavior of
    # just accepting any dict.
    ok(MyTypedDictNotTotal, {"foo": "bar"})
    ok(MyTypedDict, {"foo": "bar", "blah": "meh"})
    # Note, dicts can't have int key in JSON
    fail(Dict[int, str], {1: "2"})

    # Alias
    ok(MyDataClassAlias, MyDataClass("foo", 5, SerializableEnum.FOO))

    # IntEnum
    ok(SerializableEnum, SerializableEnum.FOO)
    ok(List[SerializableEnum], [SerializableEnum.FOO, SerializableEnum.FOO])

    # 3.10+ checks
    if sys.version_info >= (3, 10):
        ok(list[int], [1, 2])
        ok(dict[str, int], {"1": 2})
        ok(tuple[int, str], (1, "2"))

    # Pydantic
    ok(
        MyPydanticClass,
        MyPydanticClass(foo="foo", bar=[MyPydanticClass(foo="baz", bar=[])]),
    )
    ok(List[MyPydanticClass], [MyPydanticClass(foo="foo", bar=[])])
    fail(List[MyPydanticClass], [MyPydanticClass(foo="foo", bar=[]), 5])
