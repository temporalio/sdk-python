from __future__ import annotations

import pathlib
import sys
from dataclasses import dataclass
from typing import ClassVar, Dict, Optional

import pytest

from temporalio.worker.workflow_sandbox._restrictions import (
    RestrictedWorkflowAccessError,
    RestrictionContext,
    SandboxMatcher,
    SandboxRestrictions,
    _RestrictedProxy,
    _stdlib_module_names,
)


def test_workflow_sandbox_stdlib_module_names():
    if sys.version_info[1] != 11:
        pytest.skip("Test only runs on 3.11")
    actual_names = ",".join(sorted(sys.stdlib_module_names))
    # Uncomment to print code for generating these
    code_lines = [""]
    for mod_name in sorted(sys.stdlib_module_names):
        if code_lines[-1]:
            code_lines[-1] += ","
        if len(code_lines[-1]) > 80:
            code_lines.append("")
        code_lines[-1] += mod_name
    code = '_stdlib_module_names = (\n    "' + '"\n    "'.join(code_lines) + '"\n)'
    # TODO(cretz): Point releases may add modules :-(
    assert (
        actual_names == _stdlib_module_names
    ), f"Expecting names as {actual_names}. In code as:\n{code}"


def test_workflow_sandbox_restrictions_add_passthrough_modules():
    updated = SandboxRestrictions.default.with_passthrough_modules("module1", "module2")
    assert (
        "module1" in updated.passthrough_modules
        and "module2" in updated.passthrough_modules
    )


@dataclass
class RestrictableObject:
    foo: Optional[RestrictableObject] = None
    bar: int = 42
    baz: ClassVar[int] = 57
    qux: ClassVar[RestrictableObject]

    some_dict: Optional[Dict] = None


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
