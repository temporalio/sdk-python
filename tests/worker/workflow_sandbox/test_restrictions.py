from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Dict, Optional

import pytest

from temporalio.worker.workflow_sandbox.restrictions import (
    RestrictedWorkflowAccessError,
    SandboxMatcher,
    _RestrictedProxy,
)


@dataclass
class RestrictableObject:
    foo: Optional[RestrictableObject] = None
    bar: int = 42
    baz: ClassVar[int] = 57
    qux: ClassVar[RestrictableObject]

    some_dict: Optional[Dict] = None


RestrictableObject.qux = RestrictableObject(foo=RestrictableObject(bar=70), bar=80)


def test_workflow_sandbox_restricted_proxy():
    obj_class = _RestrictedProxy(
        "RestrictableObject",
        RestrictableObject,
        SandboxMatcher(
            children={
                "foo": SandboxMatcher(access={"bar"}),
                "qux": SandboxMatcher(children={"foo": SandboxMatcher(access={"foo"})}),
                "some_dict": SandboxMatcher(
                    children={
                        "key1": SandboxMatcher(access="subkey2"),
                        "key.2": SandboxMatcher(access="subkey2"),
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
