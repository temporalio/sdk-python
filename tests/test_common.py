from __future__ import annotations

import pickle
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from temporalio.api.common.v1 import Payload
from temporalio.common import (
    Priority,
    RawValue,
    RetryPolicy,
    SearchAttributeKey,
    SearchAttributePair,
    TypedSearchAttributes,
    _type_hints_from_func,
)


def test_retry_policy_validate():
    # Validation ignored for max attempts as 1
    RetryPolicy(initial_interval=timedelta(seconds=-1), maximum_attempts=1)._validate()
    with pytest.raises(ValueError, match="Initial interval cannot be negative"):
        RetryPolicy(initial_interval=timedelta(seconds=-1))._validate()
    with pytest.raises(ValueError, match="Backoff coefficient cannot be less than 1"):
        RetryPolicy(backoff_coefficient=0.5)._validate()
    with pytest.raises(ValueError, match="Maximum interval cannot be negative"):
        RetryPolicy(maximum_interval=timedelta(seconds=-1))._validate()
    with pytest.raises(
        ValueError, match="Maximum interval cannot be less than initial interval"
    ):
        RetryPolicy(
            initial_interval=timedelta(seconds=3), maximum_interval=timedelta(seconds=1)
        )._validate()
    with pytest.raises(ValueError, match="Maximum attempts cannot be negative"):
        RetryPolicy(maximum_attempts=-1)._validate()


def some_hinted_func(foo: str) -> DefinedLater:
    return DefinedLater()


async def some_hinted_func_async(foo: str) -> DefinedLater:
    return DefinedLater()


class MyCallableClass:
    def __call__(self, foo: str) -> DefinedLater:
        raise NotImplementedError

    def some_method(self, foo: str) -> DefinedLater:
        raise NotImplementedError


@dataclass
class DefinedLater:
    pass


def test_type_hints_from_func():
    def assert_hints(func: Any):
        args, return_hint = _type_hints_from_func(func)
        assert args == [str]
        assert return_hint is DefinedLater

    assert_hints(some_hinted_func)
    assert_hints(some_hinted_func_async)
    assert_hints(MyCallableClass())
    assert_hints(MyCallableClass)
    assert_hints(MyCallableClass.some_method)
    assert_hints(MyCallableClass().some_method)


def test_raw_value_pickle():
    pickled = pickle.dumps(
        RawValue(Payload(metadata={"meta-key": b"meta-val"}, data=b"data-val"))
    )
    unpickled = pickle.loads(pickled)
    assert isinstance(unpickled, RawValue)
    assert isinstance(unpickled.payload, Payload)
    assert unpickled.payload.metadata == {"meta-key": b"meta-val"}
    assert unpickled.payload.data == b"data-val"


def test_typed_search_attribute_duplicates():
    key1 = SearchAttributeKey.for_keyword("my-key1")
    key2 = SearchAttributeKey.for_int("my-key2")
    key1_dupe = SearchAttributeKey.for_int("my-key1")
    # Different keys fine
    TypedSearchAttributes(
        [SearchAttributePair(key1, "some-val"), SearchAttributePair(key2, 123)]
    )
    # Same key bad
    with pytest.raises(ValueError):
        TypedSearchAttributes(
            [SearchAttributePair(key1, "some-val"), SearchAttributePair(key1_dupe, 123)]
        )


def test_typed_search_attributes_contains_with_falsy_value():
    int_key = SearchAttributeKey.for_int("my-int")
    attrs = TypedSearchAttributes([SearchAttributePair(int_key, 0)])
    assert int_key in attrs


def test_typed_search_attributes_contains_with_truthy_value():
    int_key = SearchAttributeKey.for_int("my-int")
    attrs = TypedSearchAttributes([SearchAttributePair(int_key, 42)])
    assert int_key in attrs


def test_typed_search_attributes_contains_missing_key():
    int_key = SearchAttributeKey.for_int("my-int")
    missing_key = SearchAttributeKey.for_keyword("missing")
    attrs = TypedSearchAttributes([SearchAttributePair(int_key, 42)])
    assert missing_key not in attrs


def test_cant_construct_bad_priority():
    with pytest.raises(TypeError):
        Priority(priority_key=1.1)  # type: ignore
    with pytest.raises(ValueError):
        Priority(priority_key=-1)
