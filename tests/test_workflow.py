import uuid
from typing import Awaitable, Callable, Type, TypeVar

import pytest

from temporalio import workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.worker import Worker


class BadDefnBase:
    @workflow.signal
    def base_signal(self):
        pass

    @workflow.query
    def base_query(self):
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
    def signal3(self):
        pass

    @workflow.signal(dynamic=True)
    def signal4(self):
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
    def query3(self):
        pass

    @workflow.query(dynamic=True)
    def query4(self):
        pass

    # Intentionally missing decorator
    def base_query(self):
        pass


def test_workflow_defn_bad():
    with pytest.raises(ValueError) as err:
        workflow.defn(BadDefn)

    assert "Invalid workflow class for 7 reasons" in str(err.value)
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
    assert "@workflow.run method run must be defined on this class" in str(err.value)


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
