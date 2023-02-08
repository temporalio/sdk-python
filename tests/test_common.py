from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from temporalio.common import RetryPolicy, _type_hints_from_func


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
