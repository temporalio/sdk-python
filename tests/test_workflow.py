import inspect
import itertools
from typing import Sequence

import pytest

from temporalio import workflow
from temporalio.common import RawValue


class GoodDefnBase:
    @workflow.run
    async def run(self, name: str) -> str:
        raise NotImplementedError

    @workflow.signal
    def base_signal(self):
        pass

    @workflow.query
    def base_query(self):
        pass

    @workflow.update
    def base_update(self):
        pass


@workflow.defn(name="workflow-custom")
class GoodDefn(GoodDefnBase):
    @workflow.run
    async def run(self, name: str) -> str:
        raise NotImplementedError

    @workflow.signal
    def signal1(self):
        pass

    @workflow.signal(name="signal-custom", description="fun")
    def signal2(self):
        pass

    @workflow.signal(dynamic=True, description="boo")
    def signal3(self, name: str, args: Sequence[RawValue]):
        pass

    @workflow.query
    def query1(self):
        pass

    @workflow.query(name="query-custom", description="qd")
    def query2(self):
        pass

    @workflow.query(dynamic=True, description="dqd")
    def query3(self, name: str, args: Sequence[RawValue]):
        pass

    @workflow.update
    def update1(self):
        pass

    @workflow.update(name="update-custom", description="ud")
    def update2(self):
        pass

    @workflow.update(dynamic=True, description="dud")
    def update3(self, name: str, args: Sequence[RawValue]):
        pass


def test_workflow_defn_good():
    # Although the API is internal, we want to check the literal definition just
    # in case
    defn = workflow._Definition.from_class(GoodDefn)
    assert defn == workflow._Definition(
        name="workflow-custom",
        cls=GoodDefn,
        run_fn=GoodDefn.run,
        signals={
            "signal1": workflow._SignalDefinition(
                name="signal1", fn=GoodDefn.signal1, is_method=True
            ),
            "signal-custom": workflow._SignalDefinition(
                name="signal-custom",
                fn=GoodDefn.signal2,
                is_method=True,
                description="fun",
            ),
            None: workflow._SignalDefinition(
                name=None, fn=GoodDefn.signal3, is_method=True, description="boo"
            ),
            "base_signal": workflow._SignalDefinition(
                name="base_signal", fn=GoodDefnBase.base_signal, is_method=True
            ),
        },
        queries={
            "query1": workflow._QueryDefinition(
                name="query1", fn=GoodDefn.query1, is_method=True
            ),
            "query-custom": workflow._QueryDefinition(
                name="query-custom",
                fn=GoodDefn.query2,
                is_method=True,
                description="qd",
            ),
            None: workflow._QueryDefinition(
                name=None, fn=GoodDefn.query3, is_method=True, description="dqd"
            ),
            "base_query": workflow._QueryDefinition(
                name="base_query", fn=GoodDefnBase.base_query, is_method=True
            ),
        },
        updates={
            "update1": workflow._UpdateDefinition(
                name="update1", fn=GoodDefn.update1, is_method=True
            ),
            "update-custom": workflow._UpdateDefinition(
                name="update-custom",
                fn=GoodDefn.update2,
                is_method=True,
                description="ud",
            ),
            None: workflow._UpdateDefinition(
                name=None, fn=GoodDefn.update3, is_method=True, description="dud"
            ),
            "base_update": workflow._UpdateDefinition(
                name="base_update", fn=GoodDefnBase.base_update, is_method=True
            ),
        },
        sandboxed=True,
        failure_exception_types=[],
    )


class BadDefnBase:
    @workflow.signal
    def base_signal(self):
        pass

    @workflow.query
    def base_query(self):
        pass

    @workflow.update
    def base_update(self):
        pass


class BadDefn(BadDefnBase):
    # Intentionally missing @workflow.run

    @workflow.signal
    def signal1(self):
        pass

    @workflow.signal(name="signal1")
    def signal2(self):
        pass

    @workflow.signal(dynamic=True)
    def signal3(self, name: str, args: Sequence[RawValue]):
        pass

    @workflow.signal(dynamic=True)
    def signal4(self, name: str, args: Sequence[RawValue]):
        pass

    # Intentionally missing decorator
    def base_signal(self):
        pass

    @workflow.query
    def query1(self):
        pass

    @workflow.query(name="query1")
    def query2(self):
        pass

    @workflow.query(dynamic=True)
    def query3(self, name: str, args: Sequence[RawValue]):
        pass

    @workflow.query(dynamic=True)
    def query4(self, name: str, args: Sequence[RawValue]):
        pass

    # Intentionally missing decorator
    def base_query(self):
        pass

    @workflow.update
    def update1(self, arg1: str):
        pass

    @workflow.update(name="update1")
    def update2(self, arg1: str):
        pass

    # Intentionally missing decorator
    def base_update(self):
        pass


def test_workflow_defn_bad():
    with pytest.raises(ValueError) as err:
        workflow.defn(BadDefn)

    assert "Invalid workflow class for 9 reasons" in str(err.value)
    assert "Missing @workflow.run method" in str(err.value)
    assert (
        "Multiple signal methods found for signal1 (at least on signal2 and signal1)"
        in str(err.value)
    )
    assert (
        "Multiple signal methods found for <dynamic> (at least on signal4 and signal3)"
        in str(err.value)
    )
    assert (
        "@workflow.signal defined on BadDefnBase.base_signal but not on the override"
        in str(err.value)
    )
    assert (
        "Multiple query methods found for query1 (at least on query2 and query1)"
        in str(err.value)
    )
    assert (
        "Multiple query methods found for <dynamic> (at least on query4 and query3)"
        in str(err.value)
    )
    assert (
        "@workflow.query defined on BadDefnBase.base_query but not on the override"
        in str(err.value)
    )
    assert (
        "Multiple update methods found for update1 (at least on update2 and update1)"
        in str(err.value)
    )
    assert (
        "@workflow.update defined on BadDefnBase.base_update but not on the override"
        in str(err.value)
    )


def test_workflow_defn_local_class():
    with pytest.raises(ValueError) as err:

        @workflow.defn
        class LocalClass:
            @workflow.run
            async def run(self):
                pass

    assert "Local classes unsupported" in str(err.value)


class NonAsyncRun:
    def run(self):
        pass


def test_workflow_defn_non_async_run():
    with pytest.raises(ValueError) as err:
        workflow.run(NonAsyncRun.run)
    assert "must be an async function" in str(err.value)


class BaseWithRun:
    @workflow.run
    async def run(self):
        pass


class RunOnlyOnBase(BaseWithRun):
    pass


def test_workflow_defn_run_only_on_base():
    with pytest.raises(ValueError) as err:
        workflow.defn(RunOnlyOnBase)
    assert "@workflow.run method run must be defined on RunOnlyOnBase" in str(err.value)


class RunWithoutDecoratorOnOverride(BaseWithRun):
    async def run(self):
        pass


def test_workflow_defn_run_override_without_decorator():
    with pytest.raises(ValueError) as err:
        workflow.defn(RunWithoutDecoratorOnOverride)
    assert "@workflow.run defined on BaseWithRun.run but not on the override" in str(
        err.value
    )


class MultipleRun:
    @workflow.run
    async def run1(self):
        pass

    @workflow.run
    async def run2(self):
        pass


def test_workflow_defn_multiple_run():
    with pytest.raises(ValueError) as err:
        workflow.defn(MultipleRun)
    assert "Multiple @workflow.run methods found (at least on run2 and run1" in str(
        err.value
    )


@workflow.defn
class BadDynamic:
    @workflow.run
    async def run(self):
        pass

    # We intentionally don't decorate these here since they throw
    def some_dynamic1(self):
        pass

    def some_dynamic2(self, no_vararg):
        pass

    def old_dynamic(self, name, *args):
        pass


def test_workflow_defn_bad_dynamic():
    with pytest.raises(RuntimeError) as err:
        workflow.signal(dynamic=True)(BadDynamic.some_dynamic1)
    assert "must have 3 arguments" in str(err.value)
    with pytest.raises(RuntimeError) as err:
        workflow.signal(dynamic=True)(BadDynamic.some_dynamic2)
    assert "must have 3 arguments" in str(err.value)
    with pytest.raises(RuntimeError) as err:
        workflow.query(dynamic=True)(BadDynamic.some_dynamic1)
    assert "must have 3 arguments" in str(err.value)
    with pytest.raises(RuntimeError) as err:
        workflow.query(dynamic=True)(BadDynamic.some_dynamic2)
    assert "must have 3 arguments" in str(err.value)


def test_workflow_defn_dynamic_handler_warnings():
    with pytest.deprecated_call() as warnings:
        workflow.signal(dynamic=True)(BadDynamic.old_dynamic)
        workflow.query(dynamic=True)(BadDynamic.old_dynamic)
    assert len(warnings) == 2
    # We want to make sure they are reporting the right stacklevel
    warnings[0].filename.endswith("test_workflow.py")
    warnings[1].filename.endswith("test_workflow.py")


class _TestParametersIdenticalUpToNaming:
    def a1(self, a):
        pass

    def a2(self, b):
        pass

    def b1(self, a: int):
        pass

    def b2(self, b: int) -> str:
        return ""

    def c1(self, a1: int, a2: str) -> str:
        return ""

    def c2(self, b1: int, b2: str) -> int:
        return 0

    def d1(self, a1, a2: str) -> None:
        pass

    def d2(self, b1, b2: str) -> str:
        return ""

    def e1(self, a1, a2: str = "") -> None:
        return None

    def e2(self, b1, b2: str = "") -> str:
        return ""

    def f1(self, a1, a2: str = "a") -> None:
        return None


def test_parameters_identical_up_to_naming():
    fns = [
        f
        for _, f in inspect.getmembers(_TestParametersIdenticalUpToNaming)
        if inspect.isfunction(f)
    ]
    for f1, f2 in itertools.combinations(fns, 2):
        name1, name2 = f1.__name__, f2.__name__
        expect_equal = name1[0] == name2[0]
        assert (
            workflow._parameters_identical_up_to_naming(f1, f2) == (expect_equal)
        ), f"expected {name1} and {name2} parameters{' ' if expect_equal else ' not '}to compare equal"


@workflow.defn
class BadWorkflowInit:
    def not__init__(self):
        pass

    @workflow.run
    async def run(self):
        pass


def test_workflow_init_not__init__():
    with pytest.raises(ValueError) as err:
        workflow.init(BadWorkflowInit.not__init__)
    assert "@workflow.init may only be used on the __init__ method" in str(err.value)


class BadUpdateValidator:
    @workflow.update
    def my_update(self, a: str):
        pass

    @my_update.validator  # type: ignore
    def my_validator(self, a: int):
        pass

    @workflow.run
    async def run(self):
        pass


def test_workflow_update_validator_not_update():
    with pytest.raises(ValueError) as err:
        workflow.defn(BadUpdateValidator)
    assert (
        "Update validator method my_validator parameters do not match update method my_update parameters"
        in str(err.value)
    )
