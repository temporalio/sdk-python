from dataclasses import dataclass
from typing import Any, Callable, List, Protocol, Sequence

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

    @workflow.signal(name="signal-custom")
    def signal2(self):
        pass

    @workflow.signal(dynamic=True)
    def signal3(self, name: str, args: Sequence[RawValue]):
        pass

    @workflow.query
    def query1(self):
        pass

    @workflow.query(name="query-custom")
    def query2(self):
        pass

    @workflow.query(dynamic=True)
    def query3(self, name: str, args: Sequence[RawValue]):
        pass

    @workflow.update
    def update1(self):
        pass

    @workflow.update(name="update-custom")
    def update2(self):
        pass

    @workflow.update(dynamic=True)
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
        init_fn_takes_workflow_input=False,
        signals={
            "signal1": workflow._SignalDefinition(
                name="signal1", fn=GoodDefn.signal1, is_method=True
            ),
            "signal-custom": workflow._SignalDefinition(
                name="signal-custom", fn=GoodDefn.signal2, is_method=True
            ),
            None: workflow._SignalDefinition(
                name=None, fn=GoodDefn.signal3, is_method=True
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
                name="query-custom", fn=GoodDefn.query2, is_method=True
            ),
            None: workflow._QueryDefinition(
                name=None, fn=GoodDefn.query3, is_method=True
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
                name="update-custom", fn=GoodDefn.update2, is_method=True
            ),
            None: workflow._UpdateDefinition(
                name=None, fn=GoodDefn.update3, is_method=True
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


#
# workflow init tests
#


class CanBeCalledWithoutArgs:
    def a(self):
        pass

    def b(self, arg=1):
        pass

    def c(self, /, arg=1):
        pass

    def d(self=1):  # type: ignore
        pass

    def e(self=1, /, arg1=1, *, arg2=2):  # type: ignore
        pass


class CannotBeCalledWithoutArgs:
    def a(self, arg):
        pass

    def b(self, /, arg):
        pass

    def c(self, arg1, arg2=2):
        pass


@pytest.mark.parametrize(
    "fn,expected",
    [
        (CanBeCalledWithoutArgs.a, True),
        (CanBeCalledWithoutArgs.b, True),
        (CanBeCalledWithoutArgs.c, True),
        (CanBeCalledWithoutArgs.d, True),
        (CanBeCalledWithoutArgs.e, True),
        (CannotBeCalledWithoutArgs.a, False),
        (CannotBeCalledWithoutArgs.b, False),
        (CannotBeCalledWithoutArgs.c, False),
    ],
)
def test_unbound_method_can_be_called_without_args_when_bound(
    fn: Callable[..., Any], expected: bool
):
    assert (
        workflow._unbound_method_can_be_called_without_args_when_bound(fn) == expected
    )


class NormalInitNoInit:
    @workflow.run
    async def run(self) -> None:
        pass


class NormalInitNoInitOneParamRun:
    @workflow.run
    async def run(self, a: int) -> None:
        pass


class NormalInitNoArgInitZeroParamRun:
    def __init__(self) -> None:
        pass

    @workflow.run
    async def run(self) -> None:
        pass


class NormalInitNoArgInitOneParamRun:
    def __init__(self) -> None:
        pass

    @workflow.run
    async def run(self, a: int) -> None:
        pass


@dataclass
class MyDataClass:
    a: int
    b: str


class NormalInitDataClassProtocol(Protocol):
    @workflow.run
    async def run(self, arg: MyDataClass) -> None:
        pass


# Although the class is abstract, a user may decorate it with @workflow.defn,
# for example in order to set the name as the same as the child class, so that
# the client codebase need only import the interface.
class NormalInitAbstractBaseClass:
    def __init__(self, arg_supplied_by_child_cls) -> None:
        pass

    @workflow.run
    async def run(self) -> None: ...


class NormalInitChildClass(NormalInitAbstractBaseClass):
    def __init__(self) -> None:
        super().__init__(arg_supplied_by_child_cls=None)

    @workflow.run
    async def run(self) -> None: ...


class NormalInitSlashStarArgsStarStarKwargs:
    def __init__(self, /, *args, **kwargs) -> None:
        pass

    @workflow.run
    async def run(self) -> None:
        pass


class NormalInitStarArgsStarStarKwargs:
    def __init__(self, *args, **kwargs) -> None:
        pass

    @workflow.run
    async def run(self) -> None:
        pass


class NormalInitStarDefault:
    def __init__(self, *, arg=1) -> None:
        pass

    @workflow.run
    async def run(self, arg) -> None:
        pass


class NormalInitTypedDefault:
    def __init__(self, a: int = 1) -> None:
        pass

    @workflow.run
    async def run(self, aa: int) -> None:
        pass


@pytest.mark.parametrize(
    "cls",
    [
        NormalInitNoInit,
        NormalInitNoInitOneParamRun,
        NormalInitNoArgInitZeroParamRun,
        NormalInitNoArgInitOneParamRun,
        NormalInitDataClassProtocol,
        NormalInitSlashStarArgsStarStarKwargs,
        NormalInitStarArgsStarStarKwargs,
        NormalInitStarDefault,
        NormalInitTypedDefault,
        NormalInitAbstractBaseClass,
        NormalInitChildClass,
    ],
)
def test_workflow_init_good_does_not_take_workflow_input(cls):
    assert not workflow.defn(
        cls
    ).__temporal_workflow_definition.init_fn_takes_workflow_input


class WorkflowInitOneParamTyped:
    def __init__(self, a: int) -> None:
        pass

    @workflow.run
    async def run(self, aa: int) -> None:
        pass


class WorkflowInitTwoParamsTyped:
    def __init__(self, a: int, b: str) -> None:
        pass

    @workflow.run
    async def run(self, aa: int, bb: str) -> None:
        pass


class WorkflowInitOneParamUntyped:
    def __init__(self, a) -> None:
        pass

    @workflow.run
    async def run(self, aa) -> None:
        pass


class WorkflowInitTwoParamsUntyped:
    def __init__(self, a, b) -> None:
        pass

    @workflow.run
    async def run(self, aa, bb) -> None:
        pass


class WorkflowInitSlashStarArgsStarStarKwargs:
    def __init__(self, /, a, *args, **kwargs) -> None:
        pass

    @workflow.run
    async def run(self, /, a, *args, **kwargs) -> None:
        pass


class WorkflowInitStarArgsStarStarKwargs:
    def __init__(self, *args, a, **kwargs) -> None:
        pass

    @workflow.run
    async def run(self, *args, a, **kwargs) -> None:
        pass


class WorkflowInitStarDefault:
    def __init__(self, a, *, arg=1) -> None:
        pass

    @workflow.run
    async def run(self, a, *, arg=1) -> None:
        pass


@pytest.mark.parametrize(
    "cls",
    [
        WorkflowInitOneParamTyped,
        WorkflowInitTwoParamsTyped,
        WorkflowInitOneParamUntyped,
        WorkflowInitTwoParamsUntyped,
        WorkflowInitSlashStarArgsStarStarKwargs,
        WorkflowInitStarArgsStarStarKwargs,
        WorkflowInitStarDefault,
    ],
)
def test_workflow_init_good_takes_workflow_input(cls):
    assert workflow.defn(
        cls
    ).__temporal_workflow_definition.init_fn_takes_workflow_input


class WorkflowInitBadExtraInitParamUntyped:
    def __init__(self, a) -> None:
        pass

    @workflow.run
    async def run(self) -> None:
        pass


class WorkflowInitBadMismatchedParamUntyped:
    def __init__(self, a) -> None:
        pass

    @workflow.run
    async def run(self, aa, bb) -> None:
        pass


class WorkflowInitBadExtraInitParamTyped:
    def __init__(self, a: int) -> None:
        pass

    @workflow.run
    async def run(self) -> None:
        pass


class WorkflowInitBadMismatchedParamTyped:
    def __init__(self, a: int) -> None:
        pass

    @workflow.run
    async def run(self, aa: int, bb: str) -> None:
        pass


class WorkflowInitBadOneParamNoInitType:
    def __init__(self, a) -> None:
        pass

    @workflow.run
    async def run(self, aa: int) -> None:
        pass


class WorkflowInitBadGenericSubtype:
    # The types must match exactly; we do not support any notion of subtype
    # compatibility.
    def __init__(self, a: List) -> None:
        pass

    @workflow.run
    async def run(self, aa: List[int]) -> None:
        pass


class WorkflowInitBadOneParamNoRunType:
    def __init__(self, a: int) -> None:
        pass

    @workflow.run
    async def run(self, aa) -> None:
        pass


class WorkflowInitBadMissingDefault:
    def __init__(self, a: int, b: int) -> None:
        pass

    @workflow.run
    async def run(self, aa: int, b: int = 1) -> None:
        pass


class WorkflowInitBadInconsistentDefaults:
    def __init__(self, a: int, b: int = 1) -> None:
        pass

    @workflow.run
    async def run(self, aa: int, b: int = 2) -> None:
        pass


class WorkflowInitBadTwoParamsMixedTyping:
    def __init__(self, a, b: str) -> None:
        pass

    @workflow.run
    async def run(self, aa: str, bb) -> None:
        pass


@pytest.mark.parametrize(
    "cls",
    [
        WorkflowInitBadExtraInitParamUntyped,
        WorkflowInitBadMismatchedParamUntyped,
        WorkflowInitBadExtraInitParamTyped,
        WorkflowInitBadMismatchedParamTyped,
        WorkflowInitBadTwoParamsMixedTyping,
        WorkflowInitBadOneParamNoInitType,
        WorkflowInitBadOneParamNoRunType,
        WorkflowInitBadGenericSubtype,
        WorkflowInitBadMissingDefault,
        WorkflowInitBadInconsistentDefaults,
    ],
)
def test_workflow_init_bad_takes_workflow_input(cls):
    with pytest.raises(ValueError):
        workflow.defn(cls)
