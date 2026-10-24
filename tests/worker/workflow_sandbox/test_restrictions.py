from __future__ import annotations

import operator
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ClassVar

import pytest

from temporalio.worker.workflow_sandbox._restrictions import (
    RestrictedWorkflowAccessError,
    RestrictionContext,
    SandboxMatcher,
    SandboxRestrictions,
    _RestrictedProxy,
)


def test_workflow_sandbox_restrictions_add_passthrough_modules():
    updated = SandboxRestrictions.default.with_passthrough_modules("module1", "module2")
    assert (
        "module1" in updated.passthrough_modules
        and "module2" in updated.passthrough_modules
    )


@dataclass
class RestrictableObject:
    foo: RestrictableObject | None = None
    bar: int = 42
    baz: ClassVar[int] = 57
    qux: ClassVar[RestrictableObject]

    some_dict: dict | None = None


RestrictableObject.qux = RestrictableObject(foo=RestrictableObject(bar=70), bar=80)


class RestrictableClass:
    def __str__(self):
        return "__str__"

    def __repr__(self):
        return "__repr__"

    def __format__(self, __format_spec: str) -> str:
        return "__format__"


def test_restricted_proxy_dunder_methods():
    restricted_class = _RestrictedProxy(
        "RestrictableClass",
        RestrictableClass,
        RestrictionContext(),
        SandboxMatcher(),
    )
    restricted_obj = restricted_class()
    assert type(restricted_obj) is _RestrictedProxy
    assert str(restricted_obj) == "__str__"
    assert repr(restricted_obj) == "__repr__"
    assert format(restricted_obj, "") == "__format__"
    assert f"{restricted_obj}" == "__format__"

    restricted_path = _RestrictedProxy(
        "Path",
        pathlib.Path,
        RestrictionContext(),
        SandboxMatcher(),
    )
    assert isinstance(format(restricted_path, ""), str)
    restricted_path_obj = restricted_path("test/path")
    assert type(restricted_path_obj) is _RestrictedProxy
    expected_path = str(pathlib.PurePath("test/path"))
    assert format(restricted_path_obj, "") == expected_path
    assert f"{restricted_path_obj}" == expected_path


def test_restricted_proxy_in_place_operations_preserve_proxy():
    restricted_list = _RestrictedProxy(
        "list",
        list,
        RestrictionContext(),
        SandboxMatcher(),
    )
    values = original = restricted_list([1])

    values += [2]

    assert type(values) is _RestrictedProxy
    assert values is original
    assert RestrictionContext.unwrap_if_proxied(values) == [1, 2]


@pytest.mark.parametrize(
    "value, other, operation",
    [
        ((1,), (2,), operator.iadd),
        ((1,), 2, operator.imul),
        (frozenset({1}), {2}, operator.ior),
    ],
)
def test_restricted_proxy_in_place_operations_replace_value(
    value: Any, other: Any, operation: Callable[[Any, Any], Any]
):
    proxy = _RestrictedProxy("value", value, RestrictionContext(), SandboxMatcher())

    result = operation(proxy, other)

    assert RestrictionContext.unwrap_if_proxied(result) == operation(value, other)
    assert type(result) is _RestrictedProxy
    assert result is not proxy
    assert RestrictionContext.unwrap_if_proxied(proxy) is value


def test_restricted_proxy_in_place_operation_returns_primitive():
    value = _RestrictedProxy(
        "timedelta", timedelta(seconds=4), RestrictionContext(), SandboxMatcher()
    )

    value /= timedelta(seconds=2)

    assert value == 2.0
    assert type(value) is float


def test_workflow_sandbox_restricted_proxy():
    obj_class = _RestrictedProxy(
        "RestrictableObject",
        RestrictableObject,
        RestrictionContext(),
        SandboxMatcher(
            children={
                "foo": SandboxMatcher(access={"bar"}),
                "qux": SandboxMatcher(children={"foo": SandboxMatcher(access={"foo"})}),
                "some_dict": SandboxMatcher(
                    children={
                        "key1": SandboxMatcher(access={"subkey2"}),
                        "key.2": SandboxMatcher(access={"subkey2"}),
                    }
                ),
            }
        ),
    )
    obj = obj_class(
        foo=obj_class(),
        some_dict={
            "key1": {"subkey1": "val", "subkey2": "val"},
            "key.2": {"subkey1": "val", "subkey2": "val"},
        },
    )
    # Accessing these values is fine
    _ = (
        obj.bar,
        obj.foo.foo,
        obj_class.baz,
        obj_class.qux.bar,
        obj_class.qux.foo.bar,
        obj.some_dict["key1"]["subkey1"],
        obj.some_dict["key.2"]["subkey1"],
    )
    # But these aren't
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        _ = obj.foo.bar
    assert err.value.qualified_name == "RestrictableObject.foo.bar"
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        _ = obj_class.qux.foo.foo.bar
    assert err.value.qualified_name == "RestrictableObject.qux.foo.foo"
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        _ = obj.some_dict["key1"]["subkey2"]
    assert err.value.qualified_name == "RestrictableObject.some_dict.key1.subkey2"
    with pytest.raises(RestrictedWorkflowAccessError) as err:
        _ = obj.some_dict["key.2"]["subkey2"]
    assert err.value.qualified_name == "RestrictableObject.some_dict.key.2.subkey2"

    # Unfortunately, we can't intercept the type() call
    assert type(obj.foo) is _RestrictedProxy
