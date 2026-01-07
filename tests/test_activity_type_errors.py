"""
This file exists to test for type-checker false positives and false negatives
for the activity client API.

It doesn't contain any test functions - it uses the machinery in test_type_errors.py
to verify that pyright produces the expected errors.
"""

from datetime import timedelta
from unittest.mock import Mock

from temporalio import activity
from temporalio.client import ActivityHandle, Client
from temporalio.service import ServiceClient


@activity.defn
async def increment(x: int) -> int:
    return x + 1


@activity.defn
async def greet(name: str) -> str:
    return f"Hello, {name}"


@activity.defn
async def no_return(_: int) -> None:
    pass


@activity.defn
async def no_param_async() -> str:
    return "done"


@activity.defn
def increment_sync(x: int) -> int:
    return x + 1


@activity.defn
def no_param_sync() -> str:
    return "done"


@activity.defn
class IncrementClass:
    """Async activity defined as a callable class."""

    async def __call__(self, x: int) -> int:
        return x + 1


@activity.defn
class NoParamClass:
    """Async activity class with no parameters."""

    async def __call__(self) -> str:
        return "done"


@activity.defn
class SyncIncrementClass:
    """Sync activity defined as a callable class."""

    def __call__(self, x: int) -> int:
        return x + 1


@activity.defn
class SyncNoParamClass:
    """Sync activity class with no parameters."""

    def __call__(self) -> str:
        return "done"


class ActivityHolder:
    """Class holding activity methods."""

    @activity.defn
    async def increment_method(self, x: int) -> int:
        return x + 1

    @activity.defn
    async def no_param_method(self) -> str:
        return "done"


async def _test_start_activity_typed_callable_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[int] = await client.start_activity(
        increment,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )

    _result: int = await _handle.result()


async def _test_execute_activity_typed_callable_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: int = await client.execute_activity(
        increment,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_positional_arg_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[int] = await client.start_activity(
        increment,
        1,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_execute_activity_positional_arg_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: int = await client.execute_activity(
        increment,
        1,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_string_name_with_result_type() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle = await client.start_activity(
        "increment",
        args=[1],
        id="activity-id",
        task_queue="tq",
        result_type=int,
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_no_param_async_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[str] = await client.start_activity(
        no_param_async,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_execute_activity_no_param_async_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: str = await client.execute_activity(
        no_param_async,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_no_param_sync_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[str] = await client.start_activity(
        no_param_sync,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_execute_activity_no_param_sync_happy_path() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: str = await client.execute_activity(
        no_param_sync,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_wrong_arg_type() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[int] = await client.start_activity(
        increment,
        # assert-type-error-pyright: 'cannot be assigned to parameter'
        "wrong type",  # type: ignore
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_execute_activity_wrong_arg_type() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: int = await client.execute_activity(
        increment,
        # assert-type-error-pyright: 'cannot be assigned to parameter'
        "wrong type",  # type: ignore
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_wrong_result_type_assignment() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    handle = await client.start_activity(
        increment,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )

    # assert-type-error-pyright: 'Type "int" is not assignable to declared type "str"'
    _wrong: str = await handle.result()  # type: ignore


async def _test_execute_activity_wrong_result_type_assignment() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    # assert-type-error-pyright: 'Type "int" is not assignable to declared type "str"'
    _wrong: str = await client.execute_activity(  # type: ignore
        increment,  # type: ignore[arg-type]
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_missing_required_params() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    # assert-type-error-pyright: 'No overloads for "start_activity" match'
    await client.start_activity(  # type: ignore
        increment,
        args=[1],
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )

    # assert-type-error-pyright: 'No overloads for "start_activity" match'
    await client.start_activity(  # type: ignore
        increment,
        args=[1],
        id="activity-id",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_activity_handle_typed_correctly() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    handle_int: ActivityHandle[int] = await client.start_activity(
        increment,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )
    _int_result: int = await handle_int.result()

    handle_str: ActivityHandle[str] = await client.start_activity(
        greet,
        args=["world"],
        id="activity-id-2",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )
    _str_result: str = await handle_str.result()

    handle_none: ActivityHandle[None] = await client.start_activity(
        no_return,
        args=[1],
        id="activity-id-3",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )
    _none_result: None = await handle_none.result()  # type: ignore[func-returns-value]


async def _test_activity_handle_wrong_type_parameter() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    # assert-type-error-pyright: 'Type "ActivityHandle\[int\]" is not assignable to declared type "ActivityHandle\[str\]"'
    _handle: ActivityHandle[str] = await client.start_activity(  # type: ignore
        increment,  # type: ignore[arg-type]
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_sync_activity() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[int] = await client.start_activity(
        increment_sync,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_execute_activity_sync_activity() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: int = await client.execute_activity(
        increment_sync,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_sync_no_param() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[str] = await client.start_activity(
        no_param_sync,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


# Tests for start_activity_class and execute_activity_class
# Note: Type inference for callable classes is limited; use args= form


async def _test_start_activity_class_single_param() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[int] = await client.start_activity_class(
        IncrementClass,
        1,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_execute_activity_class_single_param() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: int = await client.execute_activity_class(
        IncrementClass,
        1,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_class_no_param() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[str] = await client.start_activity_class(
        NoParamClass,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_execute_activity_class_no_param() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: str = await client.execute_activity_class(
        NoParamClass,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


# Tests for sync callable classes


async def _test_start_activity_class_sync_single_param() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[int] = await client.start_activity_class(
        SyncIncrementClass,
        1,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_execute_activity_class_sync_single_param() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: int = await client.execute_activity_class(
        SyncIncrementClass,
        1,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_class_sync_no_param() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[str] = await client.start_activity_class(
        SyncNoParamClass,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


# Tests for start_activity_method and execute_activity_method
# Note: The _method variants work best with unbound methods (class references).
# For bound methods accessed via instance, use start_activity directly.


async def _test_start_activity_method_unbound() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    # Using unbound method reference
    _handle: ActivityHandle[int] = await client.start_activity_method(
        ActivityHolder.increment_method,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_execute_activity_method_unbound() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    # Using unbound method reference
    _result: int = await client.execute_activity_method(
        ActivityHolder.increment_method,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_start_activity_method_no_param_unbound() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    # Using unbound method reference
    _handle: ActivityHandle[str] = await client.start_activity_method(
        ActivityHolder.no_param_method,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def _test_execute_activity_method_no_param_unbound() -> None:  # type:ignore[reportUnusedFunction]
    client = Client(service_client=Mock(spec=ServiceClient))

    # Using unbound method reference
    _result: str = await client.execute_activity_method(
        ActivityHolder.no_param_method,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )
