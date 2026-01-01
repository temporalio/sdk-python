"""
This file exists to test for type-checker false positives and false negatives
for the standalone activity client API.

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
async def no_return(x: int) -> None:
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


async def test_start_activity_typed_callable_happy_path() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[int] = await client.start_activity(
        increment,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )

    _result: int = await _handle.result()


async def test_execute_activity_typed_callable_happy_path() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: int = await client.execute_activity(
        increment,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def test_start_activity_string_name_with_result_type() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle = await client.start_activity(
        "increment",
        args=[1],
        id="activity-id",
        task_queue="tq",
        result_type=int,
        start_to_close_timeout=timedelta(seconds=5),
    )


async def test_start_activity_wrong_result_type_assignment() -> None:
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


async def test_execute_activity_wrong_result_type_assignment() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    # assert-type-error-pyright: 'Type "int" is not assignable to declared type "str"'
    _wrong: str = await client.execute_activity(  # type: ignore
        increment,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def test_start_activity_missing_required_params() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    # assert-type-error-pyright: 'Argument missing for parameter "id"'
    await client.start_activity(  # type: ignore
        increment,
        args=[1],
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )

    # assert-type-error-pyright: 'Argument missing for parameter "task_queue"'
    await client.start_activity(  # type: ignore
        increment,
        args=[1],
        id="activity-id",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def test_activity_handle_typed_correctly() -> None:
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
    _none_result: None = await handle_none.result()


async def test_activity_handle_wrong_type_parameter() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    # assert-type-error-pyright: 'Type "ActivityHandle\[int\]" is not assignable to declared type "ActivityHandle\[str\]"'
    _handle: ActivityHandle[str] = await client.start_activity(  # type: ignore
        increment,
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def test_start_activity_single_arg_overload() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[int] = await client.start_activity(
        increment,
        1,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def test_execute_activity_single_arg_overload() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: int = await client.execute_activity(
        increment,
        1,
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def test_start_activity_sync_activity() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[int] = await client.start_activity(
        # assert-type-error-pyright: 'cannot be assigned to parameter "activity"'
        increment_sync,  # type: ignore
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def test_execute_activity_sync_activity() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    _result: int = await client.execute_activity(
        # assert-type-error-pyright: 'cannot be assigned to parameter "activity"'
        increment_sync,  # type: ignore
        args=[1],
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def test_start_activity_sync_no_param() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    _handle: ActivityHandle[str] = await client.start_activity(
        # assert-type-error-pyright: 'cannot be assigned to parameter "activity"'
        no_param_sync,  # type: ignore
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )


async def test_start_activity_single_arg_wrong_type() -> None:
    client = Client(service_client=Mock(spec=ServiceClient))

    await client.start_activity(
        increment,
        "not an int",
        id="activity-id",
        task_queue="tq",
        start_to_close_timeout=timedelta(seconds=5),
    )
