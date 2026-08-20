"""
This file exists to test for type-checker false positives and false negatives
for the call-site ValueHandle declaration, workflow.as_value_handle.

Declaring the intent by wrapping the callable is what keeps the deferral a
guarantee rather than a post-hoc request, and that only holds if the awaited
result is statically a ValueHandle for every call shape the workflow API
supports. It doesn't contain any test functions - the assertions are checked by
poe lint-types.
"""

from datetime import timedelta

from typing_extensions import assert_type

from temporalio import activity, workflow
from temporalio.common import ValueHandle
from temporalio.workflow import ActivityHandle

_STTC = timedelta(seconds=30)


@activity.defn
async def no_param() -> str:
    return "done"


@activity.defn
def sync_no_param() -> str:
    return "done"


@activity.defn
async def single_param(n: int) -> str:
    return str(n)


@activity.defn
async def multi_param(n: int, s: str) -> str:
    return f"{n}{s}"


@activity.defn
async def consume(data: ValueHandle[str]) -> int:
    return len(await data.get_value())


class Activities:
    @activity.defn
    async def no_param_method(self) -> str:
        return "done"

    @activity.defn
    async def single_param_method(self, n: int) -> str:
        return str(n)


@workflow.defn
class Child:
    @workflow.run
    async def run(self) -> str:
        return "done"


async def _test_activity_result_as_handle() -> None:  # type:ignore[reportUnusedFunction]
    assert_type(
        await workflow.execute_activity(
            workflow.as_value_handle(no_param), start_to_close_timeout=_STTC
        ),
        ValueHandle[str],
    )
    assert_type(
        await workflow.execute_activity(
            workflow.as_value_handle(sync_no_param), start_to_close_timeout=_STTC
        ),
        ValueHandle[str],
    )
    assert_type(
        await workflow.execute_activity(
            workflow.as_value_handle(single_param), 1, start_to_close_timeout=_STTC
        ),
        ValueHandle[str],
    )
    assert_type(
        await workflow.execute_activity(
            workflow.as_value_handle(multi_param),
            args=[1, ""],
            start_to_close_timeout=_STTC,
        ),
        ValueHandle[str],
    )
    assert_type(
        workflow.start_activity(
            workflow.as_value_handle(no_param), start_to_close_timeout=_STTC
        ),
        ActivityHandle[ValueHandle[str]],
    )


async def _test_method_and_local_activity_result_as_handle() -> None:  # type:ignore[reportUnusedFunction]
    assert_type(
        await workflow.execute_activity_method(
            workflow.as_value_handle(Activities.no_param_method),
            start_to_close_timeout=_STTC,
        ),
        ValueHandle[str],
    )
    assert_type(
        await workflow.execute_activity_method(
            workflow.as_value_handle(Activities.single_param_method),
            1,
            start_to_close_timeout=_STTC,
        ),
        ValueHandle[str],
    )
    assert_type(
        await workflow.execute_local_activity(
            workflow.as_value_handle(no_param), start_to_close_timeout=_STTC
        ),
        ValueHandle[str],
    )


async def _test_child_workflow_result_as_handle() -> None:  # type:ignore[reportUnusedFunction]
    assert_type(
        await workflow.execute_child_workflow(workflow.as_value_handle(Child.run)),
        ValueHandle[str],
    )


async def _test_undeclared_call_is_unchanged() -> None:  # type:ignore[reportUnusedFunction]
    assert_type(
        await workflow.execute_activity(no_param, start_to_close_timeout=_STTC), str
    )


async def _test_declared_result_forwards_where_the_value_was_accepted() -> None:  # type:ignore[reportUnusedFunction]
    handle = await workflow.execute_activity(
        workflow.as_value_handle(single_param), 1, start_to_close_timeout=_STTC
    )
    assert_type(
        await workflow.execute_activity(consume, handle, start_to_close_timeout=_STTC),
        int,
    )
